import torch
from torch import nn
from dataclasses import dataclass
from pathlib import Path
import json
from typing import List

from mistral.rope import precompute_freqs_cis, apply_rotary_emb
from mistral.cache import CacheView, RotatingBufferCache
from operator import itemgetter
from xformers.ops.fmha import (
    memory_efficient_attention,
)


@dataclass
class ModelArgs:
    dim: int
    n_layers: int
    head_dim: int
    hidden_dim: int
    n_heads: int
    n_kv_heads: int
    sliding_window: int
    norm_eps: float
    vocab_size: int

    max_batch_size: int = 0


def repeat_kv(keys: torch.Tensor, values: torch.Tensor, repeats: int, dim: int):
    keys = torch.repeat_interleave(keys, repeats=repeats, dim=dim)
    values = torch.repeat_interleave(values, repeats=repeats, dim=dim)
    return keys, values


class Attention(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()
        self.args = args

        self.n_heads: int = args.n_heads
        self.n_kv_heads: int = args.n_kv_heads

        self.repeats = self.n_heads // self.n_kv_heads
        self.sliding_window = self.args.sliding_window

        self.scale = self.args.head_dim ** -0.5

        self.wq = nn.Linear(
            args.dim,
            args.n_heads * args.head_dim,
            bias=False
        )
        self.wk = nn.Linear(
            args.dim,
            args.n_kv_heads * args.head_dim,
            bias=False
        )
        self.wv = nn.Linear(
            args.dim,
            args.n_kv_heads * args.head_dim,
            bias=False
        )
        self.wo = nn.Linear(
            args.n_heads * args.head_dim,
            args.dim,
            bias=False
        )

    def forward(
            self, x: torch.Tensor,
            freqs_cis: torch.Tensor,
            cache: CacheView,
    ) -> torch.Tensor:
        seqlen_sum, _ = x.shape

        xq, xk, xv = self.wq(x), self.wk(x), self.wv(x)
        xq = xq.view(seqlen_sum, self.n_heads, self.args.head_dim)
        xk = xk.view(seqlen_sum, self.n_kv_heads, self.args.head_dim)
        xv = xv.view(seqlen_sum, self.n_kv_heads, self.args.head_dim)
        xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis)

        if cache.prefill:
            key, val = cache.interleave_kv(xk, xv)
            cache.update(xk, xv)
        else:
            cache.update(xk, xv)
            key, val = cache.key, cache.value
            key = key.view(seqlen_sum * cache.sliding_window, self.n_kv_heads, self.args.head_dim)
            val = val.view(seqlen_sum * cache.sliding_window, self.n_kv_heads, self.args.head_dim)

        # Repeat keys and values to match number of query heads
        key, val = repeat_kv(key, val, self.repeats, dim=1)

        # xformers requires (B=1, S, H, D)
        xq, key, val = xq[None, ...], key[None, ...], val[None, ...]
        output = memory_efficient_attention(xq, key, val, cache.mask)

        return self.wo(output.view_as(x))


class FeedForward(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()

        self.w1 = nn.Linear(
            args.dim,
            args.hidden_dim,
            bias=False
        )
        self.w2 = nn.Linear(
            args.hidden_dim,
            args.dim,
            bias=False
        )
        self.w3 = nn.Linear(
            args.dim,
            args.hidden_dim,
            bias=False
        )

    def forward(self, x) -> torch.Tensor:
        return self.w2(nn.functional.silu(self.w1(x)) * self.w3(x))


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


class TransformerBlock(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()
        self.n_heads = args.n_heads
        self.dim = args.dim
        self.attention = None
        self.feed_forward = None
        self.attention_norm = None
        self.ffn_norm = None

        self.args = args

    def activate(self):
        args = self.args
        self.attention = Attention(args)
        self.feed_forward = FeedForward(args=args)
        self.attention_norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.ffn_norm = RMSNorm(args.dim, eps=args.norm_eps)

    def forward(
            self, x: torch.Tensor, freqs_cis: torch.Tensor, cache: CacheView
    ) -> torch.Tensor:
        r = self.attention.forward(self.attention_norm(x), freqs_cis, cache)
        h = x + r
        r = self.feed_forward.forward(self.ffn_norm(h))
        out = h + r
        return out

    def deactivate(self):
        del self.attention
        del self.feed_forward
        del self.attention_norm
        del self.ffn_norm
        self.attention = None
        self.feed_forward = None
        self.attention_norm = None
        self.ffn_norm = None


class Transformer(nn.Module):
    def __init__(self, args: ModelArgs, weights_mmap):
        super().__init__()
        self.args = args
        self.vocab_size = args.vocab_size
        self.n_layers = args.n_layers
        assert self.vocab_size > 0

        self.tok_embeddings = nn.Embedding(args.vocab_size, args.dim)
        self.layers = torch.nn.ModuleList(
            [TransformerBlock(args=args) for _ in range(args.n_layers)]
        )

        self.norm = RMSNorm(args.dim, eps=args.norm_eps)

        self.output = nn.Linear(
            args.dim,
            args.vocab_size,
            bias=False
        )

        self.tok_embeddings.load_state_dict({'weight': weights_mmap['tok_embeddings.weight']})
        self.norm.load_state_dict({'weight': weights_mmap['norm.weight']})
        self.output.load_state_dict({'weight': weights_mmap['output.weight']})
        self.freqs_cis = precompute_freqs_cis(self.args.head_dim, 128_000).to("cuda")
        self.weights_mmap = weights_mmap
        self.tok_embeddings.to('cuda')
        self.norm.to('cuda')
        self.output.to('cuda')


    @property
    def dtype(self) -> torch.dtype:
        return self.tok_embeddings.weight.dtype

    @property
    def device(self) -> torch.device:
        return self.tok_embeddings.weight.device


    def forward(
        self,
        input_ids: torch.Tensor,
        cache: RotatingBufferCache,
        seqlens: List[int],
    ) -> torch.Tensor:
        assert len(seqlens) <= self.args.max_batch_size, f"Max batch size is {self.args.max_batch_size}, got batch size of {len(seqlens)}"
        assert sum(seqlens) == input_ids.shape[0], (sum(seqlens), input_ids.shape[0])

        input_metadata = cache.get_input_metadata(seqlens)
        h = self.tok_embeddings(input_ids)
        freqs_cis = self.freqs_cis[input_metadata.positions]
        h = h.to("cuda")

        for i, layer in enumerate(self.layers):
            layer_weights = [f'layers.{i}.attention.wq.weight',
                             f'layers.{i}.attention.wk.weight',
                             f'layers.{i}.attention.wv.weight',
                             f'layers.{i}.attention.wo.weight',
                             f'layers.{i}.feed_forward.w1.weight',
                             f'layers.{i}.feed_forward.w2.weight',
                             f'layers.{i}.feed_forward.w3.weight',
                             f'layers.{i}.attention_norm.weight',
                             f'layers.{i}.ffn_norm.weight'
                             ]
            weights_dict = itemgetter(*layer_weights)(self.weights_mmap)
            weights_dict = {layer_weights[j].replace(f"layers.{i}.", ""): val for j, val in enumerate(weights_dict)}

            layer.activate()
            layer.load_state_dict(weights_dict)
            layer = layer.to("cuda")
            h = layer(h, freqs_cis, cache.get_view(i, input_metadata))
            layer.deactivate()

        cache.update_seqlens(seqlens)

        print("One iteration")
        return self.output(self.norm(h)).float()