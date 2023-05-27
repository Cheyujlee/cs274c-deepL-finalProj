import torch
import torch.nn as nn


class ProbabilisticModule(nn.Module):
    def contracting_block(self, in_channels, out_channels, kernel_size=3):
        block = torch.nn.Sequential(
            torch.nn.Conv2d(
                kernel_size=kernel_size,
                in_channels=in_channels,
                out_channels=out_channels,
            ),
            torch.nn.ReLU(),
            torch.nn.BatchNorm2d(out_channels),
            torch.nn.Conv2d(
                kernel_size=kernel_size,
                in_channels=out_channels,
                out_channels=out_channels,
            ),
            torch.nn.ReLU(),
            torch.nn.BatchNorm2d(out_channels),
        )
        return block

    def __init__(self, in_channel, out_dim):
        super().__init__()
        self.conv_encode1 = self.contracting_block(
            in_channels=in_channel, out_channels=16
        )
        self.conv_maxpool1 = torch.nn.MaxPool2d(kernel_size=3)
        self.conv_encode2 = self.contracting_block(16, 32)
        self.conv_maxpool2 = torch.nn.MaxPool2d(kernel_size=3)
        self.conv_encode3 = self.contracting_block(32, 64)
        self.conv_maxpool3 = torch.nn.MaxPool2d(kernel_size=4)
        self.linear = nn.Linear(8, out_dim)

    def forward(self, x):
        encode_block1 = self.conv_encode1(x)
        encode_pool1 = self.conv_maxpool1(encode_block1)
        encode_block2 = self.conv_encode2(encode_pool1)
        encode_pool2 = self.conv_maxpool2(encode_block2)
        encode_block3 = self.conv_encode3(encode_pool2)
        encode_pool3 = self.conv_maxpool3(encode_block3)
        prob = self.linear(encode_pool3.flatten())
        return prob


class OutputModule(nn.Module):
    def __init__(self, in_channel, out_channel) -> None:
        super().__init__()
        self.output_conv = nn.Sequential(
            torch.nn.Conv2d(
                in_channels=in_channel, out_channels=out_channel, kernel_size=1
            )
        )

    def forward(self, x):
        x = self.output_conv(x)
        return x


class ProbabilisticUNetWrapper(nn.Module):
    def __init__(
        self,
        unet: nn.Module,
        prior_net: nn.Module,
        posterior_net: nn.Module,
        output_net: nn.Module,
        latent_distribution_cls: torch.distributions.Distribution = torch.distributions.Normal,
    ) -> None:
        super().__init__()
        self.unet = unet
        self.prior_net = prior_net
        self.posterior_net = posterior_net
        self.output_net = output_net
        self.latent_distribution_cls = latent_distribution_cls

    def encode_prior(self, x) -> torch.distributions.Distribution:
        rep = self.prior_net(x)
        if isinstance(rep, tuple):
            mean, logvar = rep
        elif torch.is_tensor(rep):
            mean, logvar = torch.split(rep, rep.shape[1] // 2, dim=1)
        prior_dist: torch.distributions.Distribution = self.latent_distribution(
            mean, logvar.mul(0.5).exp()
        )
        return prior_dist

    def encode_posterior(self, x, y) -> torch.distributions.Distribution:
        rep = self.posterior_net(torch.cat((x, y.float()), 1))
        if isinstance(rep, tuple):
            mean, logvar = rep
        elif torch.is_tensor(rep):
            mean, logvar = torch.split(rep, rep.shape[1] // 2, dim=1)
        posterior_dist: torch.distributions.Distribution = self.latent_distribution(
            mean, logvar.mul(0.5).exp()
        )
        return posterior_dist

    def inject_latent_unet_forward(self, x, sample):
        repr: torch.Tensor = self.unet(x)
        expand_sample = sample[:, :, None, None].expand_as(repr)
        latent_repr = torch.cat([repr, expand_sample], dim=1)
        pred: torch.Tensor = self.output_net(latent_repr)
        return pred

    def training_forward(self, x, y):
        prior_dist = self.encode_prior(x)
        prior_sample: torch.Tensor = prior_dist.rsample()  # [b, h]

        posterior_dist = self.encode_posterior(x, y)
        posterior_sample: torch.Tensor = posterior_dist.rsample()  # [b, h]

        pred = self.inject_latent_unet_forward(x, posterior_sample)

        outputs: dict[str, torch.Tensor] = {
            "pred": pred,
            "prior_dist": prior_dist,
            "prior_sample": prior_sample,
            "posterior_dist": posterior_dist,
            "posterior_sample": posterior_sample,
        }
        return outputs

    def sampling_forward(self, x):
        prior_dist = self.encode_prior(x)
        prior_sample: torch.Tensor = prior_dist.loc  # [b, h]

        pred = self.inject_latent_unet_forward(x, prior_sample)

        outputs: dict[str, torch.Tensor] = {
            "pred": pred,
            "prior_dist": prior_dist,
            "prior_sample": prior_sample,
        }
        return outputs

    def forward(self, x, y):
        if self.training:
            outputs = self.training_forward(x, y)
        else:
            outputs = self.sampling_forward(x)
        return outputs

    def kl_divergence(self, prior, posterior):
        """Compute current KL, requires existing prior and posterior."""
        return torch.distributions.kl_divergence(posterior, prior).sum()

    @torch.inference_mode()
    def sampling_reconstruct(self, x, N=1):
        """Draw multiple samples from the current prior.

        * input_ is required if no activations are stored in task_net.
        * If input_ is given, prior will automatically be encoded again.
        * Returns either a single sample or a list of samples.

        """
        prior_dist = self.encode_prior(x)
        result = []
        result.append(self.inject_latent_unet_forward(x, prior_dist.sample()))
        while len(result) < N:
            result.append(self.inject_latent_unet_forward(x, prior_dist.sample()))

        if N == 1:
            return result[0]
        else:
            return result

    @torch.inference_mode()
    def reconstruct(self, x, posterior_dist, use_posterior_mean=True):
        """Reconstruct a sample or the current posterior mean. Will not compute gradients!"""
        if use_posterior_mean:
            sample = posterior_dist.loc
        else:
            sample = posterior_dist.sample()

        pred = self.inject_latent_unet_forward(x, sample)
        return pred

    @torch.inference_mode()
    def elbo(
        self,
        x,
        y,
        nll_reduction="sum",
        beta=1.0,
    ):
        """Compute the ELBO with seg as ground truth.

        * Prior is expected and will not be encoded.
        * If input_ is given, posterior will automatically be encoded.
        * Either input_ or stored activations must be available.

        """
        prior_dist = self.encode_prior(x)
        posterior_dist = self.encode_posterior(x, y)

        kl = self.kl_divergence(prior_dist, posterior_dist)
        nll = nn.NLLLoss(reduction=nll_reduction)(
            self.reconstruct(sample=None, use_posterior_mean=True),
            y.long(),
        )
        return -(beta * nll + kl)
