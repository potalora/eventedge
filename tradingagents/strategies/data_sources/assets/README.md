# Offline regulation prompt-planning vocabulary

`o200k_base.tiktoken` is the complete public OpenAI o200k_base BPE vocabulary,
retrieved from https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken.
SHA-256: `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`.
This URL and hash are fixed by OpenAI's tiktoken 0.12.0 source:
https://github.com/openai/tiktoken/blob/0.12.0/tiktoken_ext/openai_public.py.
The segmentation pattern in `regulation_prompt_budget.py` is from the same source.

OpenAI's MIT license is retained verbatim in `TIKTOKEN-LICENSE.txt`, from
https://raw.githubusercontent.com/openai/tiktoken/0.12.0/LICENSE.
License SHA-256: `418cb499b436128d653d79941333a5437b7be2ea9213dcc2f04d15d5d2c51d86`.

The runtime reads only this packaged, hash-verified vocabulary and constructs the
pinned tiktoken encoding directly. It does not fetch resources or use user caches.
Ordinary source text resembling special tokens is counted as ordinary text.
The 475,000-input-token gate reserves 25,000 from a 500,000 planning envelope.
These are conservative o200k_base proxy measurements, not GPT model token counts,
transport framing estimates, context entitlement or a guarantee of model acceptance.
