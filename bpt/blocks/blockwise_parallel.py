import functools
import json
import math
from functools import partial
from typing import Callable, NamedTuple, Optional

import flax.linen as nn
import jax
import jax.numpy as jnp
import numpy as np
from einops import rearrange
from flax.linen import combine_masks, make_causal_mask
from jax import lax
from jax import numpy as jnp

# Gaussian Error Linear Unit (GELU) activation function .  It takes an input tensor x and applies two operations to it: multiplying x by the sigmoid of 1.702 * x . 
def quick_gelu(x):
    return x * jax.nn.sigmoid(1.702 * x)
#The ACT2FN dictionary maps activation function names to their corresponding functions
ACT2FN = {
    "gelu": partial(nn.gelu, approximate=False),
    "relu": nn.relu,
    "silu": nn.swish,
    "swish": nn.swish,
    "gelu_new": partial(nn.gelu, approximate=True),
    "quick_gelu": quick_gelu,
}
""" 
When training a deep learning model, there are certain computations that need to be performed multiple times, such as calculating gradients. 
These computations can be memory-intensive Checkpointing helps address this issue by storing intermediate results on disk instead of keeping them all in memory. 
This way, memory usage can be reduced at the cost of additional computation time needed to read from and write to disk.
"""

def get_gradient_checkpoint_policy(name):
    return {
        'everything_saveable': jax.checkpoint_policies.everything_saveable,
        'nothing_saveable': jax.checkpoint_policies.nothing_saveable,
        'dots_saveable': jax.checkpoint_policies.dots_saveable,
        'dots_with_no_batch_dims_saveable': jax.checkpoint_policies.dots_with_no_batch_dims_saveable,
    }[name]
#to mask certain elements in computation  typically in attention mechanisms.
MASK_VALUE = -1e10
# represent the size or length of each chunk for the query and key tensors
Q_CHUNK_SIZE = 1024
K_CHUNK_SIZE = 1024
"""
The dim value controls the number of features or components used to represent each position in the input sequence. 
This function is used to generate positional embeddings for transformers.The create_sinusoidal_positions function calculates 
the sine and cosine values for each position and combines them into a 2D array representing the positional embeddings.
The inv_freq array represents the inverse frequencies used for the sinusoidal calculations. Each element of inv_freq corresponds 
to a dimension of the positional embeddings.

"""
def create_sinusoidal_positions(num_pos, dim):
    inv_freq = 1.0 / (10000 ** (np.arange(0, dim, 2) / dim))
    #performs an element-wise multiplication between np.arange(num_pos) and inv_freq.
    sinusoid_inp = np.einsum("i , j -> i j", np.arange(num_pos), inv_freq).astype("float32")
    #calculates the sine and cosine values for each element in sinusoid_inp
    sin, cos = np.sin(sinusoid_inp), np.cos(sinusoid_inp)
    #The sentinel value is computed to determine the index where the sin part ends and the cos part begins in the out array.
    sentinel = dim // 2 + dim % 2
    out = np.zeros((num_pos, dim))
    out[:, 0:sentinel] = sin
    out[:, sentinel:] = cos
    #returns the positional embeddings
    return jnp.array(out)

""" 
tensor[:, :, :, ::2] selects every other element starting from index 0 along the last dimension of the tensor.
and then negates it and stacks the two tensors together along a new axis (axis=-1). The resulting tensor will
have the shape of the original tensors, except for the added axis.for example (2, 3, 4, 5) after applying jnp.stack
we get (2, 3, 4, 2).
In simple terms, the reshape operation modifies the shape of the rotate_half_tensor tensor
"""
def rotate_every_two(tensor):
    rotate_half_tensor = jnp.stack((-tensor[:, :, :, 1::2], tensor[:, :, :, ::2]), axis=-1)
    rotate_half_tensor = rotate_half_tensor.reshape(rotate_half_tensor.shape[:-2] + (-1,))
    return rotate_half_tensor


def apply_rotary_pos_emb(tensor, sincos):
    sin_pos, cos_pos = sincos
    sin_pos = sin_pos[:, :, None, :].repeat(2, 3)
    cos_pos = cos_pos[:, :, None, :].repeat(2, 3)
    return (tensor * cos_pos) + (rotate_every_two(tensor) * sin_pos)


class _AttentionBlock(nn.Module):
    hidden_size: int
    num_heads: int
    rotary_dim: Optional[int]
    intermediate_size: int
    layer_norm_epsilon: float = 1e-5
    activation_function: str = "gelu"
    resid_pdrop: float = 0.0
    max_position_embeddings: int = 1024
    dtype: jnp.dtype = jnp.float32
    causal: bool = True
    float32_logits: bool = False

    def setup(self):
        self.embed_dim = self.hidden_size
        self.head_dim = self.embed_dim // self.num_heads
        dense = partial(
            nn.Dense,
            self.embed_dim,
            use_bias=False,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.variance_scaling(
                scale=1.0, mode='fan_in',
                distribution='normal',
            )
        )
        self.q_proj, self.k_proj, self.v_proj = dense(), dense(), dense()
        self.out_proj = dense()
        self.ln_1 = nn.LayerNorm(epsilon=self.layer_norm_epsilon, dtype=self.dtype)

        self.ln_2 = nn.LayerNorm(epsilon=self.layer_norm_epsilon, dtype=self.dtype)
        self.fc_in = nn.Dense(self.intermediate_size,
                            dtype=self.dtype,
                            kernel_init=jax.nn.initializers.variance_scaling(
                            scale=1.0, mode='fan_in',
                            distribution='normal',
            )
        )
        self.fc_out = nn.Dense(self.embed_dim,
                            dtype=self.dtype,
                            kernel_init=jax.nn.initializers.variance_scaling(
                            scale=1.0, mode='fan_in',
                            distribution='normal',
            )
        )
        self.act = ACT2FN[self.activation_function]
        self.resid_dropout = nn.Dropout(rate=self.resid_pdrop)

        if self.rotary_dim is not None and self.rotary_dim > 0:
            pos_embd_dim = self.rotary_dim
        else:
            pos_embd_dim = self.embed_dim // self.num_heads
        self.embed_positions = create_sinusoidal_positions(self.max_position_embeddings, pos_embd_dim)

    def _split_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.num_heads, self.head_dim))

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.embed_dim,))

    def attn_out_proj(self, attn_output, deterministic):
        attn_output = self._merge_heads(attn_output)
        attn_output = self.out_proj(attn_output)
        attn_output = self.resid_dropout(attn_output, deterministic=deterministic)
        return attn_output

    def forward_qkv(
        self,
        hidden_states,
        position_ids,
        deterministic: bool = True,
    ):
        hidden_states = self.ln_1(hidden_states)
        query = self.q_proj(hidden_states)
        key = self.k_proj(hidden_states)
        value = self.v_proj(hidden_states)
        query = self._split_heads(query)
        key = self._split_heads(key)
        value = self._split_heads(value)

        sincos = jnp.take(self.embed_positions, position_ids, axis=0)
        sincos = jnp.split(sincos, 2, axis=-1)
        if self.rotary_dim is not None and self.rotary_dim > 0:
            k_rot = key[:, :, :, : self.rotary_dim]
            k_pass = key[:, :, :, self.rotary_dim :]

            q_rot = query[:, :, :, : self.rotary_dim]
            q_pass = query[:, :, :, self.rotary_dim :]

            k_rot = apply_rotary_pos_emb(k_rot, sincos)
            q_rot = apply_rotary_pos_emb(q_rot, sincos)

            key = jnp.concatenate([k_rot, k_pass], axis=-1)
            query = jnp.concatenate([q_rot, q_pass], axis=-1)
        else:
            key = apply_rotary_pos_emb(key, sincos)
            query = apply_rotary_pos_emb(query, sincos)

        if self.float32_logits:
            query = query.astype(jnp.float32)
            key = key.astype(jnp.float32)

        return query, key, value

    def forward_ffn(
        self,
        hidden_states,
        deterministic: bool = True,
    ):
        hidden_states = self.ln_2(hidden_states)
        hidden_states = self.fc_in(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.fc_out(hidden_states)
        hidden_states = self.resid_dropout(hidden_states, deterministic=deterministic)

        return hidden_states


class AttentionBlock(nn.Module):
    q_chunk_size: int
    k_chunk_size: int
    hidden_size: int
    num_heads: int
    rotary_dim: Optional[int]
    intermediate_size: int
    layer_norm_epsilon: float = 1e-5
    activation_function: str = "gelu"
    attn_pdrop: float = 0.0
    resid_pdrop: float = 0.0
    max_position_embeddings: int = 1024
    dtype: jnp.dtype = jnp.float32
    causal: bool = True
    policy: str = 'nothing_saveable'
    prevent_cse: bool = False
    float32_logits: bool = False

    def setup(self):
        self.attn = _AttentionBlock(
            self.hidden_size,
            self.num_heads,
            self.rotary_dim,
            self.intermediate_size,
            self.layer_norm_epsilon,
            self.activation_function,
            self.resid_pdrop,
            self.max_position_embeddings,
            self.dtype,
            self.causal,
            self.float32_logits,
        )

    @nn.compact
    def _concatenate_to_cache(self, key, value, query, attention_mask):
        """
        This function takes projected key, value states from a single input token and concatenates the states to cached
        states from previous steps. This function is slighly adapted from the official Flax repository:
        https://github.com/google/flax/blob/491ce18759622506588784b4fca0e4bf05f8c8cd/flax/linen/attention.py#L252
        """
        # detect if we're initializing by absence of existing cache data.
        is_initialized = self.has_variable("cache", "cached_key")
        cached_key = self.variable("cache", "cached_key", jnp.zeros, key.shape, key.dtype)
        cached_value = self.variable("cache", "cached_value", jnp.zeros, value.shape, value.dtype)
        cache_index = self.variable("cache", "cache_index", lambda: jnp.array(0, dtype=jnp.int32))

        if is_initialized:
            *batch_dims, max_length, num_heads, depth_per_head = cached_key.value.shape
            # update key, value caches with our new 1d spatial slices
            cur_index = cache_index.value
            indices = (0,) * len(batch_dims) + (cur_index, 0, 0)
            key = lax.dynamic_update_slice(cached_key.value, key, indices)
            value = lax.dynamic_update_slice(cached_value.value, value, indices)
            cached_key.value = key
            cached_value.value = value
            num_updated_cache_vectors = query.shape[1]
            cache_index.value = cache_index.value + num_updated_cache_vectors
            # causal mask for cached decoder self-attention: our single query position should only attend to those key positions that have already been generated and cached, not the remaining zero elements.
            pad_mask = jnp.broadcast_to(
                jnp.arange(max_length) < cur_index + num_updated_cache_vectors,
                tuple(batch_dims) + (1, num_updated_cache_vectors, max_length),
            )
            attention_mask = combine_masks(pad_mask, attention_mask)
        return key, value, attention_mask

    def __call__(
        self,
        hidden_states,
        attention_mask,
        position_ids,
        deterministic: bool = True,
        init_cache: bool = False,
    ):
        query, key, value = self.attn.forward_qkv(hidden_states, position_ids)
        query = query / jnp.sqrt(query.shape[-1])

        dropout_rng = None
        if not deterministic and self.attn_pdrop > 0.0:
            dropout_rng = self.make_rng("dropout")

        attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

        attention_bias = lax.select(
            attention_mask > 0,
            jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
            jnp.full(attention_mask.shape, -1e9).astype(self.dtype),
        )

        # During fast autoregressive decoding, we feed one position at a time,
        # and cache the keys and values step by step.
        if self.has_variable("cache", "cached_key") or init_cache:
            query, key, value = self.attn.forward_qkv(hidden_states, position_ids)
            key, value, attention_mask = self._concatenate_to_cache(key, value, query, attention_mask)
            # use standard dot product attention since query length is 1
            attn_weights = nn.dot_product_attention_weights(
                query,
                key,
                bias=attention_bias,
                dropout_rng=dropout_rng,
                dropout_rate=self.config.attn_pdrop,
                deterministic=deterministic,
                dtype=self.dtype,
                precision=None,
            )
            attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights, value)
            attn_output = self.attn.attn_out_proj(attn_output, deterministic=deterministic)
            ffn_output = self.attn.forward_ffn(hidden_states + attn_output, deterministic=deterministic)
            outputs = attn_output + ffn_output + hidden_states
        else:
            attn_output = blockwise_compute_attn(
                query,
                key,
                value,
                bias=attention_bias,
                deterministic=not deterministic,
                dropout_rng=dropout_rng,
                attn_pdrop=self.attn_pdrop,
                causal_mask=self.causal,
                query_chunk_size=self.q_chunk_size,
                key_chunk_size=self.k_chunk_size,
                dtype=self.dtype,
                policy=self.policy,
                precision=None,
                prevent_cse=self.prevent_cse,
            )
            attn_output = self.attn.attn_out_proj(attn_output, deterministic=deterministic)
            ffn_output = blockwise_compute_ffn(
                self.attn,
                hidden_states + attn_output,
                chunk_size=self.q_chunk_size,
                deterministic=deterministic,
                policy=self.policy,
                prevent_cse=self.prevent_cse,
            )
            outputs = ffn_output + hidden_states + attn_output
        return outputs


def _chunk_attention_bias(query_chunk_size, key_chunk_size,
            bias, deterministic, attn_dropout, attn_pdrop, causal_mask,
            query_chunk_idx, key_chunk_idx):
    query_offset = query_chunk_idx * query_chunk_size
    key_offset = key_chunk_idx * key_chunk_size
    chunk_bias = jnp.zeros((1, 1, 1, 1))
    if bias is not None:
        chunk_bias = lax.dynamic_slice(
            bias,
            start_indices=(0, 0, query_offset, key_offset),
            slice_sizes=(*bias.shape[:2], min(bias.shape[-2], query_chunk_size), min(bias.shape[-1], key_chunk_size)),
        )

    if causal_mask:
        query_idx = lax.broadcasted_iota(dtype=jnp.int32, shape=(query_chunk_size, 1), dimension=0)
        key_idx = lax.broadcasted_iota(dtype=jnp.int32, shape=(1, key_chunk_size), dimension=1)
        offset = query_offset - key_offset
        query_idx += offset
        causal_mask_value = (query_idx < key_idx) * MASK_VALUE
        chunk_bias += causal_mask_value.reshape(1, 1, *causal_mask_value.shape)

    if not deterministic and attn_pdrop > 0.0:
        attn_dropout_slice = lax.dynamic_slice(
            attn_dropout,
            start_indices=(0, 0, query_offset, key_offset),
            slice_sizes=(
                *attn_dropout.shape[:2],
                min(attn_dropout.shape[-2], query_chunk_size),
                min(attn_dropout.shape[-1], key_chunk_size),
            ),
        )
        chunk_bias -= attn_dropout_slice * 1e6
    return chunk_bias

class Carry(NamedTuple):
    numerator: jax.Array
    denominator: jax.Array
    max_so_far: jax.Array

def blockwise_compute_attn(query, key, value,
        bias=None,
        deterministic=False,
        dropout_rng=None,
        attn_pdrop=0.0,
        causal_mask=True,
        query_chunk_size=None,
        key_chunk_size=None,
        dtype=jnp.float32,
        policy='nothing_saveable',
        precision=lax.Precision.HIGHEST,
        prevent_cse=False,):
    q_len = query.shape[1]
    kv_len = key.shape[1]
    query = rearrange(query, 'b (n c) h q -> b n c h q', c=query_chunk_size)
    key, value = map(lambda t: rearrange(t, 'b (n c) h v -> b n c h v', c=key_chunk_size), (key, value))
    query, key, value = map(lambda t: rearrange(t, 'b n c h d -> n b c h d'), (query, key, value))
    num_q, batch, _, num_heads, dim_per_head = query.shape
    num_kv, _, _, _, _ = key.shape

    for bias_dim, broadcast_dim in zip(bias.shape, (batch, num_heads, q_len, kv_len)):
        assert bias_dim == 1 or bias_dim == broadcast_dim
    if not deterministic and attn_pdrop > 0.0:
        attn_dropout_rng, dropout_rng = jax.random.split(dropout_rng)
        attn_dropout = jax.random.bernoulli(attn_dropout_rng, attn_pdrop, (batch, num_heads, q_len, kv_len))
    else:
        attn_dropout = None

    _chunk_bias_fn = functools.partial(
        _chunk_attention_bias,
        query_chunk_size, key_chunk_size,
        bias, deterministic, attn_dropout, attn_pdrop, causal_mask)

    def _query_chunk_attention(args):
        query_chunk, query_chunk_idx = args

        @functools.partial(jax.checkpoint, prevent_cse=prevent_cse,
                           policy=get_gradient_checkpoint_policy(policy))
        def summarize_chunk(carry, args):
            key_chunk, value_chunk, key_chunk_idx = args
            (numerator, denominator, prev_max_score) = carry
            attn_weights = jnp.einsum('bqhd,bkhd->bqhk', query_chunk, key_chunk, precision=precision)
            bias_chunk = _chunk_bias_fn(query_chunk_idx, key_chunk_idx)
            bias_chunk = jnp.moveaxis(bias_chunk, 1, 2)
            attn_weights = attn_weights + bias_chunk

            max_score = jnp.max(attn_weights, axis=-1, keepdims=True)
            max_score = jnp.maximum(prev_max_score, max_score)
            max_score = jax.lax.stop_gradient(max_score)
            exp_weights = jnp.exp(attn_weights - max_score)
            exp_values = jnp.einsum(
                'bqhv,bvhf->bqhf', exp_weights, value_chunk, precision=precision
            )
            correction = jnp.exp(prev_max_score - max_score)
            numerator = numerator * correction + exp_values
            denominator = denominator * correction + exp_weights.sum(axis=-1, keepdims=True)
            return Carry(numerator, denominator, max_score), None

        init_carry = Carry(
            jnp.zeros((batch, query_chunk_size, num_heads, dim_per_head), dtype=dtype),
            jnp.zeros((batch, query_chunk_size, num_heads, dim_per_head), dtype=dtype),
            (-jnp.inf) * jnp.ones((batch, query_chunk_size, num_heads, 1), dtype=dtype),
        )
        (numerator, denominator, max_score), _ = lax.scan(
            summarize_chunk, init_carry, xs=(key, value, jnp.arange(0, num_kv))
        )
        outputs = (numerator / denominator).astype(dtype)
        return outputs

    _, res = lax.scan(
        lambda _, x: ((), _query_chunk_attention(x)),
        (), xs=(query, jnp.arange(0, num_q))
    )
    res = rearrange(res, 'n b c h d -> b (n c) h d')
    return res

def blockwise_compute_ffn(cell, inputs, chunk_size, deterministic, policy, prevent_cse):
    inputs = rearrange(inputs, 'b (n c) d -> b n c d', c=chunk_size)
    inputs = rearrange(inputs, 'b n c d -> n b c d')
    num_q, _, _, _ = inputs.shape
    def ffn(cell, _, hidden_states):
        outputs = cell.forward_ffn(hidden_states, deterministic=deterministic)
        return _, outputs
    ffn_remat = nn.remat(
        ffn,
        variables="params",
        rngs={"params" : False},
        prevent_cse=prevent_cse,
        policy=get_gradient_checkpoint_policy(policy),
    )
    _, res = nn.scan(
        ffn_remat,
        variable_broadcast="params",
        split_rngs={"params": False},
        in_axes=0,
        out_axes=0,
        length=num_q,
    )(cell, None, inputs)
    res = rearrange(res, 'n b c d -> b (n c) d')
    return res

class Blockwise_LM_Head(nn.Module):
    vocab_size: int
    chunk_size: int
    policy: str = 'nothing_saveable'
    dtype: jnp.dtype = jnp.float32
    prevent_cse: bool = False

    def setup(self):
        self.lm_head = nn.Dense(
            self.vocab_size,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.variance_scaling(
                scale=1.0, mode='fan_in',
                distribution='normal',
            )
        )

    def __call__(self, inputs):
        inputs = rearrange(inputs, 'b (n c) d -> b n c d', c=self.chunk_size)
        inputs = rearrange(inputs, 'b n c d -> n b c d')
        num_q, _, _, _ = inputs.shape
        def lm_head(cell, _, hidden_states):
            outputs = cell(hidden_states)
            return _, outputs
        lm_head_remat = nn.remat(
            lm_head,
            variables="params",
            rngs={"params" : False},
            prevent_cse=self.prevent_cse,
            policy=get_gradient_checkpoint_policy(self.policy),
        )
        _, res = nn.scan(
            lm_head_remat,
            variable_broadcast="params",
            split_rngs={"params": False},
            in_axes=0,
            out_axes=0,
            length=num_q,
        )(self.lm_head, None, inputs)
        res = rearrange(res, 'n b c d -> b (n c) d')
        return res

def blockwise_cross_entropy(logits, tokens, valid=None,
                            chunk_size=None, policy=None, prevent_cse=None):
    if valid is None:
        valid = jnp.ones(tokens.shape[:2])
    valid = valid.astype(jnp.float32)
    logits = jnp.reshape(logits, (-1, logits.shape[-1]))
    tokens = jnp.reshape(tokens, (-1,))
    valid = jnp.reshape(valid, (-1,))

    def _cross_entropy_loss_and_accuracy(logits, tokens, valid):
        valid_text_length = jnp.maximum(jnp.sum(valid, axis=-1), 1e-10)

        token_log_prob = jnp.squeeze(
            jnp.take_along_axis(
                jax.nn.log_softmax(logits, axis=-1),
                jnp.expand_dims(tokens, -1),
                axis=-1,
            ),
            -1,
        )
        token_log_prob = jnp.where(valid > 0.0, token_log_prob, jnp.array(0.0))
        correct = jnp.where(
            valid > 0.0,
            jnp.argmax(logits, axis=-1) == tokens,
            jnp.array(False)
        )
        return token_log_prob, correct, valid_text_length
    @partial(jax.checkpoint, prevent_cse=prevent_cse,
             policy=get_gradient_checkpoint_policy(policy))
    def _loss_and_accuracy(carry, args):
        loss, accuracy, num = carry
        logits, tokens, valid = args
        token_log_prob, correct, valid_text_length = \
            _cross_entropy_loss_and_accuracy(logits, tokens, valid)
        loss = loss + jnp.sum(token_log_prob, axis=-1) / valid_text_length
        accuracy = accuracy + jnp.sum(correct, axis=-1) / valid_text_length
        num = num + 1
        return (loss, accuracy, num), None
    num_chunk = logits.shape[0] // chunk_size
    logits = rearrange(logits, '(n c) d -> n c d', c=chunk_size)
    tokens = rearrange(tokens, '(n c) -> n c', c=chunk_size)
    valid = rearrange(valid, '(n c) -> n c', c=chunk_size)
    (loss, accuracy, num), _ = jax.lax.scan(
        _loss_and_accuracy, (0.0, 0.0, 0), xs=(logits, tokens, valid),
        length=num_chunk,
    )
    loss = - loss / num
    accuracy = accuracy / num
    return loss, accuracy

if __name__ == '__main__':
    with jax.profiler.trace('/tmp/prof/blockwise_parallel_simplified'):
        class Model(nn.Module):
            def setup(self):
                self.blocks = [
                    AttentionBlock(
                        q_chunk_size=256,
                        k_chunk_size=256,
                        hidden_size=2048,
                        num_heads=16,
                        rotary_dim=128,
                        intermediate_size=8192,
                        layer_norm_epsilon=1e-5,
                        activation_function="gelu",
                        resid_pdrop=0.0,
                        max_position_embeddings=2048,
                        dtype=jnp.float32,
                        causal=True,
                )
                for _ in range(2)
                ]
            def __call__(self, hidden_states, attention_mask, position_ids):
                for block in self.blocks:
                    hidden_states = block(hidden_states, attention_mask, position_ids)
                return hidden_states

        hidden_states = jnp.zeros((2, 1024, 2048))
        attention_mask = jnp.zeros((2, 1024), dtype=jnp.int32)
        position_ids = jnp.zeros((2, 1024), dtype=jnp.int32)
        model = Model()
        variables = model.init(jax.random.PRNGKey(0), hidden_states, attention_mask, position_ids)
        output = model.apply(variables, hidden_states, attention_mask, position_ids)
        output = output.block_until_ready()
