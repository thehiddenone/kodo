# Laguna-S-2.1 — catalog notes

Every entry offers the shared knobs plus one private one, `context-laguna`
(`LAGUNA_CONTEXT_KNOB` in `_knobs_laguna.py`), reaching 512K or 1M tokens on
top of a 256K default. That module's docstring explains why this knob is
built by hand rather than with `make_yarn_context_knob`: every Laguna-S-2.1
quant ships with YaRN rope-scaling already baked into its GGUF metadata.

This family used to carry a lot more. Each of its twenty quants shipped eight
predefined flavors — `default`, five fixed sampling presets, and two context
variants — which between them enumerated the handful of combinations someone
had thought to write down. All eight are gone: the sampling presets became the
shared `tail-culling` and `temperature` knobs (two independent axes now, so
"strong culling at a low temperature" — a combination no preset offered — is
simply two dropdowns), and the context variants became `context-laguna`.
`doc/QUANT_SAMPLING.md` still explains which sampling settings suit which
quant; it is guidance, not something baked into per-quant values, and never
was (an earlier revision tiered the presets by quantization severity, which
was speculative and unmeasured, and was removed).
