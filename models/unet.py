import torch
import torch.utils.checkpoint as checkpoint
import torch.nn as nn
import torch.nn.functional as F
from .attention import MultiheadSelfAttention
from .activation_fn import GeGELU
from typing import Optional, List


class UNet_TransformerEncoder(nn.Module):
    def __init__(self, num_heads: int, embedding_dim: int, cond_dim: int, use_lora: bool):
        super().__init__()
        channels = embedding_dim * num_heads
        self.groupnorm = nn.GroupNorm(32, channels)
        self.conv_input = nn.Conv2d(channels, channels, kernel_size=1, padding=0)

        self.transformer_block = UNet_AttentionBlock(num_heads=num_heads, embedding_dim=channels, cond_dim=cond_dim, use_lora=use_lora)
        
        self.conv_output = nn.Conv2d(channels, channels, kernel_size=1, padding=0)
        
    def forward(self, x: torch.Tensor, cond: torch.Tensor=None) -> torch.Tensor:
        # x: (b, c, h, w)
        b, c, h, w = x.shape
        
        x_in = x

        x = self.groupnorm(x)
        x = self.conv_input(x)

        # (b, c, h, w) -> (b, c, h * w) -> (b, h * w, c)
        x = x.view(b, c, -1).transpose(-1, -2)
        x = self.transformer_block(x=x, cond=cond)

        x = x.transpose(-1, -2).view(b, c, h, w)

        x = self.conv_output(x)

        x += x_in
        
        return x
        
class UNet_AttentionBlock(nn.Module):
    def __init__(self, num_heads: int, embedding_dim: int, cond_dim: int, use_lora: bool=False):
        super().__init__()
        
        if embedding_dim % num_heads:
            raise ValueError('Number of heads must be divisible by Embedding Dimension')
            
        self.head_dim = embedding_dim // num_heads

        self.layernorm_1 = nn.LayerNorm(embedding_dim)
        self.attn1 = MultiheadSelfAttention(num_heads=num_heads, embedding_dim=embedding_dim, cond_dim=None, qkv_bias=False)
        
        self.layernorm_2 = nn.LayerNorm(embedding_dim)
        self.attn2 = MultiheadSelfAttention(num_heads=num_heads, embedding_dim=embedding_dim, cond_dim=cond_dim, qkv_bias=False)

        self.layernorm_3 = nn.LayerNorm(embedding_dim)
                
        self.ffn = nn.Sequential(
            GeGELU(embedding_dim, embedding_dim * 4),
            nn.Linear(embedding_dim * 4, embedding_dim))

        if use_lora:
            self.attn1.proj_q.parametrizations.weight[0].enabled = True
            self.attn1.proj_k.parametrizations.weight[0].enabled = True
            self.attn1.proj_v.parametrizations.weight[0].enabled = True
            self.attn1.proj_out.parametrizations.weight[0].enabled = True
            
            self.attn2.proj_q.parametrizations.weight[0].enabled = True
            self.attn2.proj_k.parametrizations.weight[0].enabled = True
            self.attn2.proj_v.parametrizations.weight[0].enabled = True
            self.attn2.proj_out.parametrizations.weight[0].enabled = True
        
        self.gradient_checkpointing = False
    
    def forward(self, x: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        residual_x = x
        x = self.layernorm_1(x)
        if self.gradient_checkpointing:
            x = checkpoint.checkpoint(self.attn1, x, use_reentrant=False)
        else:
            x = self.attn1(x)
        x += residual_x
        
        residual_x = x
        x = self.layernorm_2(x)
        if self.gradient_checkpointing:
            x = checkpoint.checkpoint(self.attn2, x, cond, use_reentrant=False)
        else:
            x = self.attn2(x, cond=cond)
        x += residual_x
        
        residual_x = x
        x = self.layernorm_3(x)
        x = self.ffn(x)

        x += residual_x
        
        return x
        

class UNet_ResBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, t_embed_dim: int):
        super().__init__()
        
        self.groupnorm_1 = nn.GroupNorm(num_groups=32, num_channels=in_channels)
        self.conv_1 = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=3, stride=1, padding=1)

        self.groupnorm_2 = nn.GroupNorm(num_groups=32, num_channels=out_channels)
        self.conv_2 = nn.Conv2d(in_channels=out_channels, out_channels=out_channels, kernel_size=3, stride=1, padding=1)
        
        self.t_embed = nn.Linear(t_embed_dim, out_channels)
        
        if in_channels == out_channels:
            self.proj_input = nn.Identity()
        else:
            self.proj_input = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=1, padding=0)

        self.silu_1 = nn.SiLU()
        self.silu_2 = nn.SiLU()
        self.silu_t_embed = nn.SiLU()

    def forward(self, x: torch.Tensor, t_embed: torch.Tensor) -> torch.Tensor:
        # x: (n, c, h, w)
        h = self.groupnorm_1(x)
        h = self.silu_1(h)
        h = self.conv_1(h)
        
        # time: (1, t_embed_dim) -> (1, out_channels)
        time = self.silu_t_embed(t_embed)
        time = self.t_embed(time)
        # (n, out_channels, h, w) + (1, out_channels, 1, 1) -> (n, out_channels, h, w)
        h += time[:, :, None, None]

        h = self.groupnorm_2(h)
        h = self.silu_2(h)
        h = self.conv_2(h)

        x = self.proj_input(x)
        
        h += x
        return h

class TimeEmbedding(nn.Module):
    def __init__(self, t_embed_dim: int=320):
        super().__init__()
        
        self.t_embed_dim = t_embed_dim
        self.ffn = nn.Sequential(
            # (1, 320) -> (1, 1280)
            nn.Linear(t_embed_dim, t_embed_dim * 4),
            nn.SiLU(),
            # (1, 1280) -> (1, 1280)
            nn.Linear(t_embed_dim * 4,  t_embed_dim * 4))

    def _get_time_embedding(self, timestep: torch.LongTensor):
        half = self.t_embed_dim // 2
        freqs = torch.pow(10000, -torch.arange(0, half, dtype=torch.long)/half)
        x = timestep[:, None] * freqs[None, :].to(timestep.device)
        return torch.cat([torch.cos(x), torch.sin(x)], dim=-1).type(torch.get_default_dtype())
            
    def forward(self, timestep: torch.LongTensor) -> torch.Tensor:
        t_embed = self._get_time_embedding(timestep)
        return self.ffn(t_embed)

class TimeStepSequential(nn.Sequential):
    def forward(self, x: torch.Tensor, t_embed: torch.Tensor, cond=None) -> torch.Tensor:
        for layer in self:
            if isinstance(layer, UNet_ResBlock):
                x = layer(x, t_embed)
            elif isinstance(layer, UNet_TransformerEncoder):
                x = layer(x, cond)
            else:
                x = layer(x)
        return x
        
class UNet_Downsample(nn.Module):
    def __init__(self, in_channels: int):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=2, padding=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.conv(x)

class UNet_Upsample(nn.Module):
    def __init__(self, in_channels: int):
        super().__init__()
        # self.upsample = nn.Upsample(scale_factor=2)
        self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1)

    def forward(self, x: torch.Tensor, upscale=True) -> torch.Tensor:
        if upscale:
            x = F.interpolate(x, scale_factor=2, mode='nearest')
        return self.conv(x)
    
class UNet_Encoder(nn.Module):
    def __init__(self, in_channels: int=4, num_heads: int=8, t_embed_dim: int=1280, cond_dim: int=768, ch_multiplier=[1, 2, 4, 4], use_lora: bool=False):
        super().__init__()
        ch = 320
        
        self.conv_in = nn.Conv2d(in_channels, ch, kernel_size=3, stride=1, padding=1)
        
        self.down = nn.ModuleList()
        in_ch_multiplier = [1] + ch_multiplier
        
        for i in range(len(ch_multiplier)):
            down = nn.Module()
            in_channels = ch * in_ch_multiplier[i]
            out_channels = ch * ch_multiplier[i]
            
            if i != len(ch_multiplier) - 1:
                block = nn.Sequential(
                    TimeStepSequential(UNet_ResBlock(in_channels, out_channels, t_embed_dim), UNet_TransformerEncoder(num_heads=num_heads, embedding_dim=out_channels // num_heads, cond_dim=cond_dim, use_lora=use_lora)),
                    TimeStepSequential(UNet_ResBlock(out_channels, out_channels, t_embed_dim), UNet_TransformerEncoder(num_heads=num_heads, embedding_dim=out_channels // num_heads, cond_dim=cond_dim, use_lora=use_lora)))
                downsample = UNet_Downsample(out_channels)
            else:
                block = nn.Sequential(
                TimeStepSequential(UNet_ResBlock(in_channels, out_channels, t_embed_dim)),
                TimeStepSequential(UNet_ResBlock(out_channels, out_channels, t_embed_dim)))
                
                downsample = nn.Identity()
            
            down.block = block
            down.downsample = downsample
            
            self.down.append(down)
       
    def forward(self, x: torch.Tensor, t_embed: torch.Tensor, cond: Optional[torch.Tensor]) -> torch.Tensor:
        x = self.conv_in(x)
        skip_connections = [x]
        for down in self.down:
            for layer in down.block:
                x = layer(x, t_embed, cond)
                skip_connections.append(x)
                
            x = down.downsample(x)
            if not isinstance(down.downsample, nn.Identity):
                skip_connections.append(x)
                
        return x, skip_connections

class UNet_Decoder(nn.Module):
    def __init__(self, num_heads: int=8, t_embed_dim: int=1280, cond_dim: int=768, ch_multiplier=[1, 2, 4, 4], use_lora: bool=False):
        super().__init__()
        ch = 320
        decoder_channels = ch_multiplier + [4]
        
        self.up = nn.ModuleList()
        for i in reversed(range(len(ch_multiplier))):
            up = nn.Module()
            in_ch = decoder_channels[i + 1] * ch
            out_ch = decoder_channels[i] * ch
            if i > 0:
                mid_ch = decoder_channels[i-1] * ch
            else:
                mid_ch = ch
            if i == len(ch_multiplier) - 1:
                block = nn.Sequential(
                    TimeStepSequential(UNet_ResBlock(in_ch + out_ch, out_ch, t_embed_dim)),
                    TimeStepSequential(UNet_ResBlock(out_ch + out_ch, out_ch, t_embed_dim)),
                    TimeStepSequential(UNet_ResBlock(out_ch + mid_ch, out_ch, t_embed_dim)))
            else:
                block = nn.Sequential(
                    TimeStepSequential(UNet_ResBlock(in_ch + out_ch, out_ch, t_embed_dim), UNet_TransformerEncoder(num_heads=num_heads, embedding_dim=out_ch // num_heads, cond_dim=cond_dim, use_lora=use_lora)), 
                    TimeStepSequential(UNet_ResBlock(out_ch + out_ch, out_ch, t_embed_dim), UNet_TransformerEncoder(num_heads=num_heads, embedding_dim=out_ch // num_heads, cond_dim=cond_dim, use_lora=use_lora)),
                    TimeStepSequential(UNet_ResBlock(out_ch + mid_ch, out_ch, t_embed_dim), UNet_TransformerEncoder(num_heads=num_heads, embedding_dim=out_ch // num_heads, cond_dim=cond_dim, use_lora=use_lora)))
            
            if i != 0:
                upsample = UNet_Upsample(out_ch)
            else:
                upsample = nn.Identity()

            up.block = block
            up.upsample = upsample

            self.up.append(up)

            

    def forward(self, x: torch.Tensor, skip_connections: List[torch.Tensor], t_embed: torch.Tensor, cond: Optional[torch.Tensor]) -> torch.Tensor:
        # x: (b, c, h, w)
        for up in self.up:
            prev_hw = skip_connections[-1].shape[-1]
            for layer in up.block:
                tmp = skip_connections.pop()
                x = torch.cat([x, tmp], dim=1)
                x = layer(x, t_embed, cond)
                
            if skip_connections and skip_connections[-1].shape[-1] == prev_hw:
                x = up.upsample(x, upscale=False)
            else:
                x = up.upsample(x)
            
        return x

class UNet(nn.Module):
    def __init__(self, in_channels: int=4, out_channels: int=4, num_heads: int=8, t_embed_dim: int=320, cond_dim: int=768, use_lora=False):
        super().__init__()
        
        self.time_embedding = TimeEmbedding(t_embed_dim)
        self.encoder = UNet_Encoder(in_channels=in_channels, num_heads=num_heads, t_embed_dim=t_embed_dim * 4, cond_dim=cond_dim, use_lora=use_lora)
        self.bottleneck = TimeStepSequential(
            UNet_ResBlock(1280, 1280, t_embed_dim * 4),
            UNet_TransformerEncoder(num_heads=8, embedding_dim=160, cond_dim=cond_dim, use_lora=use_lora),
            UNet_ResBlock(1280, 1280, t_embed_dim * 4)
        )
        self.decoder = UNet_Decoder(num_heads=num_heads, t_embed_dim=t_embed_dim * 4, cond_dim=cond_dim, use_lora=use_lora)
        self.output = nn.Sequential(
            nn.GroupNorm(32, 320),
            nn.SiLU(),
            nn.Conv2d(320, out_channels, kernel_size=3, stride=1, padding=1))
        
        
    def gradient_checkpointing_enabled(self, enabled=False):
        for name, module in self.encoder.named_modules():
            if isinstance(module, UNet_AttentionBlock):
                module.gradient_checkpointing = enabled
                
        for name, module in self.bottleneck.named_modules():
            if isinstance(module, UNet_AttentionBlock):
                module.gradient_checkpointing = enabled
                
        for name, module in self.decoder.named_modules():
            if isinstance(module, UNet_AttentionBlock):
                module.gradient_checkpointing = enabled
    
    def enable_flash_attn(self):
        for name, module in self.encoder.named_modules():
            if isinstance(module, MultiheadSelfAttention):
                module.use_flash_attention = True
                
        for name, module in self.bottleneck.named_modules():
            if isinstance(module, MultiheadSelfAttention):
                module.use_flash_attention = True
                
        for name, module in self.decoder.named_modules():
            if isinstance(module, MultiheadSelfAttention):
                module.use_flash_attention = True
        
                
    def forward(self, x: torch.Tensor, timestep: torch.LongTensor, cond: torch.Tensor) -> torch.Tensor:
        # t: (n,) -> (n, 1280)
        t_embed = self.time_embedding(timestep)
        
        x, skip_connections = self.encoder(x, t_embed, cond)
        
        
        x = self.bottleneck(x, t_embed, cond)
        
        
        x = self.decoder(x, skip_connections, t_embed, cond)

        output = self.output(x)
        return output
        