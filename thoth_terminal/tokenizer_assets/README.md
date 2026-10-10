This directory bundles OpenAI's official `cl100k_base` BPE vocabulary for offline
reference token accounting. It does not measure exact Muse/provider model tokens.

Source: https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken

SHA256: `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`

The hash and encoding parameters are published in:
https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py

The loader verifies the hash before constructing the tokenizer and never accesses
the network. `LICENSE.tiktoken` carries the upstream MIT license from:
https://github.com/openai/tiktoken/blob/main/LICENSE
