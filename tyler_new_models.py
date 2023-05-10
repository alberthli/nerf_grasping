import torch
import torch.nn as nn
from typing import List, Tuple, Optional
from dataclasses import dataclass
from omegaconf import MISSING
from FiLM_resnet import resnet18, ResNet18_Weights
from FiLM_resnet_1d import ResNet1D
from torchvision.transforms import Lambda, Compose
from enum import Enum, auto
from functools import partial, cached_property
from positional_encodings.torch_encodings import PositionalEncoding1D, Summer


class ConvOutputTo1D(Enum):
    FLATTEN = auto()  # (N, C, H, W) -> (N, C*H*W)
    AVG_POOL_SPATIAL = auto()  # (N, C, H, W) -> (N, C, 1, 1) -> (N, C)
    AVG_POOL_CHANNEL = auto()  # (N, C, H, W) -> (N, 1, H, W) -> (N, H*W)
    MAX_POOL_SPATIAL = auto()  # (N, C, H, W) -> (N, C, 1, 1) -> (N, C)
    MAX_POOL_CHANNEL = auto()  # (N, C, H, W) -> (N, 1, H, W) -> (N, H*W)


class PoolType(Enum):
    MAX = auto()
    AVG = auto()


def mlp(
    num_inputs: int,
    num_outputs: int,
    hidden_layers: List[int],
    activation=nn.ReLU,
    output_activation=nn.Identity,
) -> nn.Sequential:
    layers = []
    layer_sizes = [num_inputs] + hidden_layers + [num_outputs]
    for i in range(len(layer_sizes) - 1):
        act = activation if i < len(layer_sizes) - 2 else output_activation
        layers += [nn.Linear(layer_sizes[i], layer_sizes[i + 1]), act()]
    return nn.Sequential(*layers)


class Mean(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.mean(x, dim=self.dim)


class Max(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.max(x, dim=self.dim)


CHANNEL_DIM = 1
CONV_2D_OUTPUT_TO_1D_MAP = {
    ConvOutputTo1D.FLATTEN: nn.Identity,
    ConvOutputTo1D.AVG_POOL_SPATIAL: partial(nn.AdaptiveAvgPool2d, output_size=(1, 1)),
    ConvOutputTo1D.AVG_POOL_CHANNEL: partial(Mean, dim=CHANNEL_DIM),
    ConvOutputTo1D.MAX_POOL_SPATIAL: partial(nn.AdaptiveMaxPool2d, output_size=(1, 1)),
    ConvOutputTo1D.MAX_POOL_CHANNEL: partial(Max, dim=CHANNEL_DIM),
}
CONV_1D_OUTPUT_TO_1D_MAP = {
    ConvOutputTo1D.FLATTEN: nn.Identity,
    ConvOutputTo1D.AVG_POOL_SPATIAL: partial(nn.AdaptiveAvgPool1d, output_size=1),
    ConvOutputTo1D.AVG_POOL_CHANNEL: partial(Mean, dim=CHANNEL_DIM),
    ConvOutputTo1D.MAX_POOL_SPATIAL: partial(nn.AdaptiveMaxPool1d, output_size=1),
    ConvOutputTo1D.MAX_POOL_CHANNEL: partial(Max, dim=CHANNEL_DIM),
}


def conv_encoder(
    input_shape: Tuple[int, ...],
    conv_channels: List[int],
    pool_type: PoolType = PoolType.MAX,
    dropout_prob: float = 0.0,
    conv_output_to_1d: ConvOutputTo1D = ConvOutputTo1D.FLATTEN,
    activation=nn.ReLU,
) -> nn.Module:
    # Input: Either (n_channels, n_dims) or (n_channels, height, width) or (n_channels, depth, height, width)

    # Validate input
    assert 2 <= len(input_shape) <= 4
    n_input_channels = input_shape[0]
    n_spatial_dims = len(input_shape[1:])

    # Layers for different input sizes
    n_spatial_dims_to_conv_layer_map = {1: nn.Conv1d, 2: nn.Conv2d, 3: nn.Conv3d}
    n_spatial_dims_to_maxpool_layer_map = {
        1: nn.MaxPool1d,
        2: nn.MaxPool2d,
        3: nn.MaxPool3d,
    }
    n_spatial_dims_to_avgpool_layer_map = {
        1: nn.AvgPool1d,
        2: nn.AvgPool2d,
        3: nn.AvgPool3d,
    }
    n_spatial_dims_to_dropout_layer_map = {
        # 1: nn.Dropout1d,  # Not in some versions of torch
        2: nn.Dropout2d,
        3: nn.Dropout3d,
    }
    n_spatial_dims_to_adaptivemaxpool_layer_map = {
        1: nn.AdaptiveMaxPool1d,
        2: nn.AdaptiveMaxPool2d,
        3: nn.AdaptiveMaxPool3d,
    }
    n_spatial_dims_to_adaptiveavgpool_layer_map = {
        1: nn.AdaptiveMaxPool1d,
        2: nn.AdaptiveMaxPool2d,
        3: nn.AdaptiveMaxPool3d,
    }

    # Setup layer types
    conv_layer = n_spatial_dims_to_conv_layer_map[n_spatial_dims]
    if pool_type == PoolType.MAX:
        pool_layer = n_spatial_dims_to_maxpool_layer_map[n_spatial_dims]
    elif pool_type == PoolType.AVG:
        pool_layer = n_spatial_dims_to_avgpool_layer_map[n_spatial_dims]
    else:
        raise ValueError(f"Invalid pool_type = {pool_type}")
    dropout_layer = n_spatial_dims_to_dropout_layer_map[n_spatial_dims]

    # Conv layers
    layers = []
    n_channels = [n_input_channels] + conv_channels
    for i in range(len(n_channels) - 1):
        layers += [
            conv_layer(
                in_channels=n_channels[i],
                out_channels=n_channels[i + 1],
                kernel_size=3,
                stride=1,
                padding="same",
            ),
            activation(),
            pool_layer(kernel_size=2, stride=2),
        ]
        if dropout_prob != 0.0:
            layers += [dropout_layer(p=dropout_prob)]

    # Convert from (n_channels, X) => (Y,)
    if conv_output_to_1d == ConvOutputTo1D.FLATTEN:
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.AVG_POOL_SPATIAL:
        adaptiveavgpool_layer = n_spatial_dims_to_adaptiveavgpool_layer_map[
            n_spatial_dims
        ]
        layers.append(
            adaptiveavgpool_layer(output_size=tuple([1 for _ in range(n_spatial_dims)]))
        )
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.MAX_POOL_SPATIAL:
        adaptivemaxpool_layer = n_spatial_dims_to_adaptivemaxpool_layer_map[
            n_spatial_dims
        ]
        layers.append(
            adaptivemaxpool_layer(output_size=tuple([1 for _ in range(n_spatial_dims)]))
        )
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.AVG_POOL_CHANNEL:
        channel_dim = 1
        layers.append(Mean(dim=channel_dim))
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.MAX_POOL_CHANNEL:
        channel_dim = 1
        layers.append(Max(dim=channel_dim))
        layers.append(nn.Flatten(start_dim=1))
    else:
        raise ValueError(f"Invalid conv_output_to_1d = {conv_output_to_1d}")

    return nn.Sequential(*layers)


class FiLMGenerator(nn.Module):
    num_beta_gamma = 2  # one scale and one bias

    def __init__(
        self, film_input_dim: int, num_params_to_film: int, hidden_layers: List[int]
    ) -> None:
        super().__init__()
        self.film_input_dim = film_input_dim
        self.num_params_to_film = num_params_to_film
        self.film_output_dim = self.num_beta_gamma * num_params_to_film

        self.mlp = mlp(
            num_inputs=self.film_input_dim,
            num_outputs=self.film_output_dim,
            hidden_layers=hidden_layers,
        )

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        assert len(x.shape) == 2
        assert x.shape[1] == self.film_input_dim
        batch_size = x.shape[0]

        # Use delta-gamma so baseline is gamma=1
        film_output = self.mlp(x)
        beta, delta_gamma = torch.chunk(film_output, chunks=self.num_beta_gamma, dim=1)
        gamma = delta_gamma + 1.0
        assert beta.shape == gamma.shape == (batch_size, self.num_params_to_film)

        return beta, gamma


class ConvEncoder2D(nn.Module):
    def __init__(
        self,
        input_shape: Tuple[int, int, int],
        conditioning_dim: Optional[int] = None,
        use_resnet: bool = True,
        use_pretrained: bool = True,
        pooling_method: ConvOutputTo1D = ConvOutputTo1D.FLATTEN,
    ) -> None:
        super().__init__()

        # input_shape: (n_channels, height, width)
        self.input_shape = input_shape
        self.conditioning_dim = conditioning_dim
        self.use_resnet = use_resnet
        self.use_pretrained = use_pretrained
        self.pooling_method = pooling_method

        assert len(input_shape) == 3
        n_channels, height, width = input_shape
        assert n_channels == 1

        # Create conv architecture
        if self.use_resnet:
            # TODO: Properly config
            weights = ResNet18_Weights.DEFAULT if self.use_pretrained else None
            weights_transforms = weights.transforms() if self.use_pretrained else []
            self.img_preprocess = Compose(
                [
                    Lambda(lambda x: x.repeat(1, 3, 1, 1)),
                    weights_transforms,
                ]
            )
            self.conv_2d = resnet18(weights=weights)
            self.conv_2d.avgpool = CONV_2D_OUTPUT_TO_1D_MAP[self.pooling_method]()
            self.conv_2d.fc = nn.Identity()
        else:
            # TODO: Properly config
            self.img_preprocess = nn.Identity()
            self.conv_2d = conv_encoder(
                input_shape=input_shape,
                conv_channels=[32, 64, 128, 256],
                pool_type=PoolType.MAX,
                dropout_prob=0.0,
                conv_output_to_1d=self.pooling_method,
                activation=nn.ReLU,
            )

        # Create FiLM generator
        if self.conditioning_dim is not None and self.num_film_params is not None:
            # TODO: Properly config
            self.film_generator = FiLMGenerator(
                film_input_dim=self.conditioning_dim,
                num_params_to_film=self.num_film_params,
                hidden_layers=[64, 64],
            )

    def forward(
        self,
        x: torch.Tensor,
        conditioning: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        # x: (batch_size, n_channels, height, width)
        assert len(x.shape) == 4
        batch_size, _, _, _ = x.shape

        # Ensure valid use of conditioning
        assert (conditioning is None and self.conditioning_dim is None) or (
            conditioning is not None
            and self.conditioning_dim is not None
            and conditioning.shape == (batch_size, self.conditioning_dim)
        )

        # FiLM
        if conditioning is not None:
            beta, gamma = self.film_generator(conditioning)
            assert (
                beta.shape == gamma.shape == (batch_size, self.conv_2d.num_film_params)
            )
        else:
            beta, gamma = None, None

        # Conv
        x = self.img_preprocess(x)
        x = self.conv_2d(x, beta=beta, gamma=gamma)
        assert len(x.shape) == 2 and x.shape[0] == batch_size

        return x

    @property
    def num_film_params(self) -> Optional[int]:
        return (
            self.conv_2d.num_film_params
            if hasattr(self.conv_2d, "num_film_params")
            else None
        )

    @cached_property
    def output_dim(self) -> int:
        # Compute output shape
        example_input = torch.randn(1, *self.input_shape)
        example_conditioning = (
            torch.randn(1, self.conditioning_dim)
            if self.conditioning_dim is not None
            else None
        )
        example_output = self(example_input, conditioning=example_conditioning)
        assert len(example_output.shape) == 2
        return example_output.shape[1]


class ConvEncoder1D(nn.Module):
    def __init__(
        self,
        input_shape: Tuple[int, int],
        conditioning_dim: Optional[int] = None,
        use_resnet: bool = True,
        pooling_method: ConvOutputTo1D = ConvOutputTo1D.FLATTEN,
    ) -> None:
        super().__init__()

        # input_shape: (n_channels, seq_len)
        self.input_shape = input_shape
        self.conditioning_dim = conditioning_dim
        self.use_resnet = use_resnet
        self.pooling_method = pooling_method

        assert len(input_shape) == 2
        n_channels, seq_len = input_shape

        # Create conv architecture
        if self.use_resnet:
            # TODO: Properly config
            self.conv_1d = ResNet1D(
                in_channels=n_channels,
                seq_len=seq_len,
                base_filters=64,
                kernel_size=3,
                stride=1,
                groups=1,
                n_block=4,
                n_classes=1,
                downsample_gap=2,
                increasefilter_gap=2,
                use_do=False,
                verbose=False,
            )
            # Set equivalent pooling setting
            self.conv_1d.avgpool = CONV_1D_OUTPUT_TO_1D_MAP[self.pooling_method]()
            self.conv_1d.fc = nn.Identity()
        else:
            # TODO: Properly config
            self.conv_1d = conv_encoder(
                input_shape=input_shape,
                conv_channels=[32, 64, 128, 256],
                pool_type=PoolType.MAX,
                dropout_prob=0.0,
                conv_output_to_1d=ConvOutputTo1D.FLATTEN,
                activation=nn.ReLU,
            )

        # Create FiLM generator
        if self.conditioning_dim is not None and self.num_film_params is not None:
            # TODO: Properly config
            self.film_generator = FiLMGenerator(
                film_input_dim=self.conditioning_dim,
                num_params_to_film=self.num_film_params,
                hidden_layers=[64, 64],
            )

    def forward(
        self,
        x: torch.Tensor,
        conditioning: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        # x: (batch_size, n_channels, seq_len)
        assert len(x.shape) == 3
        batch_size, _, _ = x.shape

        # Ensure valid use of conditioning
        assert (conditioning is None and self.conditioning_dim is None) or (
            conditioning is not None
            and self.conditioning_dim is not None
            and conditioning.shape == (batch_size, self.conditioning_dim)
        )

        # FiLM
        if conditioning is not None:
            beta, gamma = self.film_generator(conditioning)
            assert (
                beta.shape == gamma.shape == (batch_size, self.conv_1d.num_film_params)
            )
        else:
            beta, gamma = None, None

        # Conv
        x = self.conv_1d(x, beta=beta, gamma=gamma)
        assert len(x.shape) == 2 and x.shape[0] == batch_size

        return x

    @property
    def num_film_params(self) -> Optional[int]:
        return self.conv_1d.num_film_params if self.use_resnet else None

    @cached_property
    def output_dim(self) -> int:
        # Compute output shape
        example_input = torch.randn(1, *self.input_shape)
        example_conditioning = (
            torch.randn(1, self.conditioning_dim)
            if self.conditioning_dim is not None
            else None
        )
        example_output = self(example_input, conditioning=example_conditioning)
        assert len(example_output.shape) == 2
        return example_output.shape[1]


class Conv2DtoConv1D(nn.Module):
    def __init__(
        self,
        input_shape: Tuple[int, int, int],
        conditioning_dim: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.input_shape = input_shape
        self.conditioning_dim = conditioning_dim
        assert len(input_shape) == 3

        n_channels, height, width = input_shape
        seq_len = n_channels

        # Create conv encoders
        self.n_channels_for_conv_2d = 1
        self.conv_encoder_2d = ConvEncoder2D(
            input_shape=(self.n_channels_for_conv_2d, height, width),
            conditioning_dim=conditioning_dim,
        )
        self.conv_encoder_1d = ConvEncoder1D(
            input_shape=(self.conv_encoder_2d.output_dim, seq_len),
            conditioning_dim=conditioning_dim,
        )

    def forward(
        self, x: torch.Tensor, conditioning: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        assert len(x.shape) == 4
        batch_size, n_channels, height, width = x.shape
        seq_len = n_channels

        # Ensure valid use of conditioning
        assert (self.conditioning_dim is None and conditioning is None) or (
            self.conditioning_dim is not None
            and conditioning is not None
            and conditioning.shape == (batch_size, self.conditioning_dim)
        )

        # Conv 2d
        # Reshape to (batch_size * seq_len, n_channels, height, width)
        # TODO: this can easily OOM, probably find a nice way around this
        x = x.reshape(batch_size * seq_len, self.n_channels_for_conv_2d, height, width)
        if conditioning is not None:
            conditioning_repeated = conditioning.repeat(seq_len, 1)
            x = self.conv_encoder_2d(x, conditioning=conditioning_repeated)
        else:
            x = self.conv_encoder_2d(x)
        assert x.shape == (batch_size * seq_len, self.conv_encoder_2d.output_dim)

        # Conv 1d
        # Reshape to (batch_size, output_dim, seq_len)
        x = x.reshape(batch_size, seq_len, self.conv_encoder_2d.output_dim).permute(
            (0, 2, 1)
        )
        assert x.shape == (batch_size, self.conv_encoder_2d.output_dim, seq_len)
        x = self.conv_encoder_1d(x, conditioning=conditioning)
        assert len(x.shape) == 2 and x.shape[0] == batch_size

        return x

    @property
    def output_dim(self) -> int:
        return self.conv_encoder_1d.output_dim


class TransformerEncoder1D(nn.Module):
    def __init__(
        self,
        input_shape: Tuple[int, int],
        conditioning_dim: Optional[int] = None,
        pooling_method: ConvOutputTo1D = ConvOutputTo1D.FLATTEN,
        n_emb: int = 128,
        p_drop_emb: float = 0.1,
        p_drop_attn: float = 0.1,
    ) -> None:
        super().__init__()

        # input_shape: (n_channels, seq_len)
        self.input_shape = input_shape
        self.conditioning_dim = conditioning_dim
        self.pooling_method = pooling_method
        self.n_emb = n_emb

        n_channels, seq_len = input_shape

        # Encoder
        self.encoder_input_emb = nn.Linear(self.encoder_input_dim, n_emb)
        self.encoder_pos_emb = Summer(PositionalEncoding1D(channels=n_emb))
        self.encoder_drop_emb = nn.Dropout(p=p_drop_emb)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=n_emb,
            nhead=4,
            dim_feedforward=4 * n_emb,
            dropout=p_drop_attn,
            activation="gelu",
            batch_first=True,
            norm_first=True,
        )
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer=encoder_layer, num_layers=4
        )

        if conditioning_dim is not None:
            # Decoder
            self.decoder_input_emb = nn.Linear(n_channels, n_emb)
            self.decoder_pos_emb = Summer(PositionalEncoding1D(channels=n_emb))
            self.decoder_drop_emb = nn.Dropout(p=p_drop_emb)
            decoder_layer = nn.TransformerDecoderLayer(
                d_model=n_emb,
                nhead=4,
                dim_feedforward=4 * n_emb,
                dropout=p_drop_attn,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.transformer_decoder = nn.TransformerDecoder(
                decoder_layer=decoder_layer, num_layers=4
            )

        self.ln_f = nn.LayerNorm(n_emb)
        self.pool = CONV_1D_OUTPUT_TO_1D_MAP[self.pooling_method]()

    def forward(
        self, x: torch.Tensor, conditioning: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        batch_size = x.shape[0]
        n_channels, seq_len = self.input_shape
        assert x.shape == (batch_size, n_channels, seq_len)

        assert (
            conditioning is not None and self.conditioning_dim is not None
        ) or (conditioning is None and self.conditioning_dim is None)
        if conditioning is not None and self.conditioning_dim is not None:
            assert conditioning.shape == (batch_size, self.conditioning_dim)

            # Condition encoder
            # Need to repeat conditioning to match seq_len
            conditioning = conditioning.reshape(
                batch_size,
                1,
                self.conditioning_dim,
            ).repeat(1, seq_len, 1)
            assert conditioning.shape == (
                batch_size,
                seq_len,
                self.conditioning_dim,
            )
            conditioning = self._encoder(conditioning)
            assert conditioning.shape == (batch_size, seq_len, self.n_emb)

            # Decoder
            x = x.permute(0, 2, 1)
            assert x.shape == (batch_size, seq_len, n_channels)
            x = self._decoder(x, conditioning)
            assert x.shape == (batch_size, seq_len, self.n_emb)
        else:
            # Encoder
            x = x.permute(0, 2, 1)
            assert x.shape == (batch_size, seq_len, n_channels)
            x = self._encoder(x)
            assert x.shape == (batch_size, seq_len, self.n_emb)

        x = self.ln_f(x)
        assert x.shape == (batch_size, seq_len, self.n_emb)

        # Need to permute to (batch_size, n_channels, seq_len) for pooling
        x = x.permute(0, 2, 1)
        x = self.pool(x)
        x = x.flatten(start_dim=1)

        assert len(x.shape) == 2 and x.shape[0] == batch_size

        return x

    def _encoder(self, x: torch.Tensor) -> torch.Tensor:
        batch_size = x.shape[0]
        _, seq_len = self.input_shape
        assert x.shape == (batch_size, seq_len, self.encoder_input_dim)

        x = self.encoder_input_emb(x)
        x = self.encoder_pos_emb(x)
        x = self.encoder_drop_emb(x)
        x = self.transformer_encoder(x)

        return x

    def _decoder(self, x: torch.Tensor, conditioning: torch.Tensor) -> torch.Tensor:
        batch_size = x.shape[0]
        n_channels, seq_len = self.input_shape
        assert x.shape == (batch_size, seq_len, n_channels)

        x = self.decoder_input_emb(x)
        x = self.decoder_pos_emb(x)
        x = self.decoder_drop_emb(x)
        x = self.transformer_decoder(tgt=x, memory=conditioning)

        return x

    @cached_property
    def output_dim(self) -> int:
        # Compute output shape
        example_input = torch.randn(1, *self.input_shape)
        example_conditioning = (
            torch.randn(1, self.conditioning_dim)
            if self.conditioning_dim is not None
            else None
        )
        example_output = self(example_input, conditioning=example_conditioning)
        assert len(example_output.shape) == 2
        return example_output.shape[1]

    @property
    def encoder_input_dim(self) -> int:
        n_channels, _ = self.input_shape
        return (
            self.conditioning_dim
            if self.conditioning_dim is not None
            else n_channels
        )


class Conv2DtoTransformer1D(nn.Module):
    def __init__(
        self,
        input_shape: Tuple[int, int, int],
        conditioning_dim: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.input_shape = input_shape
        self.conditioning_dim = conditioning_dim
        assert len(input_shape) == 3

        n_channels, height, width = input_shape
        seq_len = n_channels

        # Create conv encoders
        self.n_channels_for_conv_2d = 1
        self.conv_encoder_2d = ConvEncoder2D(
            input_shape=(self.n_channels_for_conv_2d, height, width),
            conditioning_dim=conditioning_dim,
        )
        self.transformer_encoder_1d = TransformerEncoder1D(
            input_shape=(self.conv_encoder_2d.output_dim, seq_len),
            conditioning_dim=conditioning_dim,
        )

    def forward(
        self, x: torch.Tensor, conditioning: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        assert len(x.shape) == 4
        batch_size, n_channels, height, width = x.shape
        seq_len = n_channels

        # Ensure valid use of conditioning
        assert (self.conditioning_dim is None and conditioning is None) or (
            self.conditioning_dim is not None
            and conditioning is not None
            and conditioning.shape == (batch_size, self.conditioning_dim)
        )

        # Conv 2d
        # Reshape to (batch_size * seq_len, n_channels, height, width)
        # TODO: this can easily OOM, probably find a nice way around this
        x = x.reshape(batch_size * seq_len, self.n_channels_for_conv_2d, height, width)
        if conditioning is not None:
            conditioning_repeated = conditioning.repeat(seq_len, 1)
            x = self.conv_encoder_2d(x, conditioning=conditioning_repeated)
        else:
            x = self.conv_encoder_2d(x)
        assert x.shape == (batch_size * seq_len, self.conv_encoder_2d.output_dim)

        # Transformer 1d
        # Reshape to (batch_size, output_dim, seq_len)
        x = x.reshape(batch_size, seq_len, self.conv_encoder_2d.output_dim).permute(
            (0, 2, 1)
        )
        assert x.shape == (batch_size, self.conv_encoder_2d.output_dim, seq_len)
        x = self.transformer_encoder_1d(x, conditioning=conditioning)
        assert len(x.shape) == 2 and x.shape[0] == batch_size

        return x

    @property
    def output_dim(self) -> int:
        return self.transformer_encoder_1d.output_dim


if __name__ == "__main__":
    from torchinfo import summary

    # Create model
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    batch_size, n_channels, height, width = (
        5,
        10,
        30,
        40,
    )  # WARNING: Easy to OOM
    conv_2d_to_conv_1d = Conv2DtoConv1D(input_shape=(n_channels, height, width)).to(
        device
    )

    example_input = torch.randn(batch_size, n_channels, height, width, device=device)
    example_output = conv_2d_to_conv_1d(example_input)
    print("Conv2DtoConv1D")
    print("=" * 80)
    print(f"example_input.shape = {example_input.shape}")
    print(f"example_output.shape = {example_output.shape}")
    print()

    # Create model 2
    conditioning_dim = 10
    conv_2d_to_conv_1d_film = Conv2DtoConv1D(
        input_shape=(n_channels, height, width),
        conditioning_dim=conditioning_dim,
    ).to(device)
    example_conditioning = torch.randn(
        batch_size, conditioning_dim, device=device
    )
    example_film_output = conv_2d_to_conv_1d_film(
        example_input, example_conditioning
    )
    print("Conv2DtoConv1D FiLM")
    print("=" * 80)
    print(f"example_input.shape = {example_input.shape}")
    print(f"example_conditioning.shape = {example_conditioning.shape}")
    print(f"example_film_output.shape = {example_film_output.shape}")
    print()

    # Create model 3
    conditioning_dim = 10
    conv_2d_to_transformer_1d = Conv2DtoTransformer1D(
        input_shape=(n_channels, height, width)
    ).to(device)
    example_transformer_output = conv_2d_to_transformer_1d(example_input)
    print("Conv2DtoTransformer1D")
    print("=" * 80)
    print(f"example_input.shape = {example_input.shape}")
    print(f"example_transformer_output.shape = {example_transformer_output.shape}")
    print()

    # Create model 4
    conditioning_dim = 10
    conv_2d_to_transformer_1d_conditioned = Conv2DtoTransformer1D(
        input_shape=(n_channels, height, width),
        conditioning_dim=conditioning_dim,
    ).to(device)
    example_transformer_output_conditioned = conv_2d_to_transformer_1d_conditioned(
        example_input, example_conditioning
    )
    print("Conv2DtoTransformer1D Conditioned")
    print("=" * 80)
    print(f"example_input.shape = {example_input.shape}")
    print(f"example_conditioning.shape = {example_conditioning.shape}")
    print(
        f"example_transformer_output_conditioned.shape = {example_transformer_output_conditioned.shape}"
    )
    print()
