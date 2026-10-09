#!/usr/bin/env python3
"""
Train Whisper on your own radio: Cambridge Fire / Pro EMS voices, unit names, Harvard buildings.

Uses the clips the crew marked Right or corrected on the dashboard (logs/training). Steps:
  1. Split the clips: ~85% to learn from, ~15% held back as a test the model never sees.
  2. Score the model the server uses now on the held-back clips (word error rate).
  3. Fine-tune a copy of the base model with LoRA (small add-on weights; fast, low memory, hard to break).
  4. Convert it to the Mac-GPU (MLX) format the server runs, in models/finetuned-<date>-mlx.
  5. Score the new model on the same held-back clips.
  6. Only if it is clearly better (3%+ fewer errors) is it switched on (models/active-model.txt).
     Restart the server to use it. `python train_whisper.py --revert` goes back to the stock model.

Run it with the server stopped (both need the GPU):  ./train.command   or   .venv/bin/python train_whisper.py
First run installs PyTorch / transformers / peft and downloads the base model (~1.6 GB).
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import random
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TRAIN_DIR = os.path.join(HERE, "logs", "training")
MODELS = os.path.join(HERE, "models")
ACTIVE = os.path.join(MODELS, "active-model.txt")
DEFAULT_MLX = os.path.join(MODELS, "large-v3-turbo-mlx")
DEFAULT_HF = "openai/whisper-large-v3-turbo"


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------- data
def load_clips():
    latest = {}
    try:
        with open(os.path.join(TRAIN_DIR, "clips.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    latest[r["key"]] = r
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    out = []
    for r in latest.values():
        path = os.path.join(TRAIN_DIR, r.get("audio") or "")
        if r.get("audio") and os.path.isfile(path) and (r.get("text") or "").strip():
            out.append({"key": r["key"], "audio": path, "text": r["text"].strip(), "verdict": r["verdict"],
                        "heard": r.get("heard", "")})
    return sorted(out, key=lambda c: c["key"])


def split(clips, test_pct=15):
    """Deterministic: a clip stays in the same half every run, so scores stay comparable."""
    h = lambda c: int(hashlib.md5(c["key"].encode()).hexdigest(), 16) % 100
    test = [c for c in clips if h(c) < test_pct]
    train = [c for c in clips if h(c) >= test_pct]
    return train, test


def decode(path):
    import av
    import numpy as np
    chunks = []
    rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(path) as c:
        for fr in c.decode(audio=0):
            for f in rs.resample(fr):
                chunks.append(f.to_ndarray().reshape(-1))
        for f in rs.resample(None):
            chunks.append(f.to_ndarray().reshape(-1))
    return (np.concatenate(chunks).astype(np.float32) / 32768.0) if chunks else np.zeros(0, np.float32)


def prepared(path):
    """Exactly what the live server feeds Whisper (enhance.py)."""
    import enhance
    return enhance.enhance(decode(path))


# ---------------------------------------------------------------------------- scoring
def corpus_wer(rows):
    from feedback import wer, words
    ref_words = sum(len(words(r["ref"])) for r in rows) or 1
    return sum(wer(r["ref"], r["hyp"]) * len(words(r["ref"])) for r in rows) / ref_words


def eval_mlx(model_dir, test, prompt):
    import mlx_whisper
    rows = []
    for c in test:
        r = mlx_whisper.transcribe(prepared(c["audio"]), path_or_hf_repo=model_dir, language="en", verbose=None,
                                   initial_prompt=prompt or None, temperature=0.0, condition_on_previous_text=False)
        rows.append({"key": c["key"], "ref": c["text"], "hyp": (r.get("text") or "").strip()})
    return corpus_wer(rows), rows


# ---------------------------------------------------------------------------- HF -> MLX
def remap_key(k):
    k = k.replace("model.", "", 1) if k.startswith("model.") else k
    for a, b in ((".layers.", ".blocks."), (".self_attn_layer_norm", ".attn_ln"), (".self_attn.", ".attn."),
                 (".encoder_attn_layer_norm", ".cross_attn_ln"), (".encoder_attn.", ".cross_attn."),
                 (".final_layer_norm", ".mlp_ln"), (".q_proj", ".query"), (".k_proj", ".key"), (".v_proj", ".value"),
                 (".out_proj", ".out"), (".fc1", ".mlp1"), (".fc2", ".mlp2"),
                 ("decoder.embed_positions.weight", "decoder.positional_embedding"),
                 ("decoder.embed_tokens", "decoder.token_embedding"),
                 ("encoder.layer_norm", "encoder.ln_post"), ("decoder.layer_norm", "decoder.ln")):
        k = k.replace(a, b)
    return k


def hf_to_mlx(model, out_dir, dtype="float16"):
    """Write a Hugging Face Whisper model in the folder format mlx-whisper loads (config.json + weights.safetensors)."""
    import mlx.core as mx
    cfg = model.config
    dims = {"n_mels": cfg.num_mel_bins, "n_audio_ctx": cfg.max_source_positions, "n_audio_state": cfg.d_model,
            "n_audio_head": cfg.encoder_attention_heads, "n_audio_layer": cfg.encoder_layers, "n_vocab": cfg.vocab_size,
            "n_text_ctx": cfg.max_target_positions, "n_text_state": cfg.d_model,
            "n_text_head": cfg.decoder_attention_heads, "n_text_layer": cfg.decoder_layers}
    weights = {}
    for k, v in model.state_dict().items():
        if k == "proj_out.weight" or "encoder.embed_positions" in k:
            continue                     # output layer is tied to the token embedding; encoder positions are fixed sinusoids
        a = v.detach().float().cpu().numpy()
        nk = remap_key(k)
        if "conv" in nk and nk.endswith("weight"):
            a = a.transpose(0, 2, 1)     # PyTorch Conv1d (out, in, k) -> MLX (out, k, in)
        weights[nk] = mx.array(a).astype(getattr(mx, dtype))
    os.makedirs(out_dir, exist_ok=True)
    mx.save_safetensors(os.path.join(out_dir, "weights.safetensors"), weights)
    with open(os.path.join(out_dir, "config.json"), "w") as f:
        json.dump({"model_type": "whisper", **dims}, f, indent=2)
    return dims, sorted(weights)


# ---------------------------------------------------------------------------- fine-tuning
def finetune(hf_base, train, args):
    import numpy as np
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    dev = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    say(f"Loading {hf_base} on {dev}...")
    processor = WhisperProcessor.from_pretrained(args.processor or hf_base)
    model = WhisperForConditionalGeneration.from_pretrained(hf_base, torch_dtype=torch.float32)
    english_only = model.config.vocab_size < 51865
    tok = processor.tokenizer
    if english_only:
        tok.set_prefix_tokens(predict_timestamps=False)
    else:
        tok.set_prefix_tokens(language="english", task="transcribe", predict_timestamps=False)
    model.config.forced_decoder_ids = None
    model.generation_config.forced_decoder_ids = None
    model.config.use_cache = False
    if not args.no_checkpointing:   # recompute activations instead of storing them: ~3x less memory, ~30% slower
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    start_id = model.config.decoder_start_token_id

    lora = LoraConfig(r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05,
                      target_modules=["q_proj", "k_proj", "v_proj", "out_proj"])
    model = get_peft_model(model, lora)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    say(f"LoRA: training {trainable / 1e6:.1f}M of {sum(p.numel() for p in model.parameters()) / 1e6:.0f}M weights")
    model.to(dev)

    say(f"Preparing {len(train)} training clips...")
    items = []
    rng = random.Random(0)
    for c in train:
        audio = prepared(c["audio"])
        feats = processor.feature_extractor(audio, sampling_rate=16000, return_tensors="np").input_features[0]
        ids = tok(c["text"]).input_ids
        if ids and ids[0] == start_id:
            ids = ids[1:]                  # the model adds the start token itself when given labels
        items.append((feats.astype(np.float32), ids[:440]))

    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr, weight_decay=0.01)
    steps = max(1, (len(items) + args.batch - 1) // args.batch) * args.epochs
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / max(1, steps // 10)) * max(0.05, 1 - s / steps))
    model.train()
    step = 0
    for ep in range(args.epochs):
        rng.shuffle(items)
        losses, t0 = [], time.time()
        for i in range(0, len(items), args.batch):
            batch = items[i:i + args.batch]
            feats = torch.tensor(np.stack([b[0] for b in batch]), device=dev)
            n = max(len(b[1]) for b in batch)
            labels = torch.full((len(batch), n), -100, dtype=torch.long)
            for j, (_, ids) in enumerate(batch):
                labels[j, :len(ids)] = torch.tensor(ids)
            out = model(input_features=feats, labels=labels.to(dev))
            out.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            losses.append(float(out.loss))
            step += 1
            if args.max_steps and step >= args.max_steps:
                break
        say(f"  epoch {ep + 1}/{args.epochs}: loss {sum(losses) / max(1, len(losses)):.3f}  ({time.time() - t0:.0f} s)")
        if args.max_steps and step >= args.max_steps:
            break
    model = model.merge_and_unload()
    model.eval()
    return model.to("cpu")


# ---------------------------------------------------------------------------- main
def current_model():
    try:
        with open(ACTIVE) as f:
            p = f.read().strip()
        if p and os.path.isdir(p if os.path.isabs(p) else os.path.join(HERE, p)):
            return p if os.path.isabs(p) else os.path.join(HERE, p)
    except OSError:
        pass
    return DEFAULT_MLX


def main():
    ap = argparse.ArgumentParser(description="Fine-tune Whisper on the crew's corrected radio clips")
    ap.add_argument("--hf-base", default=DEFAULT_HF, help="Hugging Face model to fine-tune (same family as the server's model)")
    ap.add_argument("--processor", default=None, help="(testing) folder with the tokenizer / feature extractor")
    ap.add_argument("--current", default=None, help="MLX model folder to compare against (default: what the server uses)")
    ap.add_argument("--min-clips", type=int, default=100, help="refuse to train with fewer clips than this")
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=16, help="LoRA rank")
    ap.add_argument("--no-checkpointing", action="store_true", help="faster, but needs much more memory")
    ap.add_argument("--max-steps", type=int, default=0, help="(testing) stop after this many steps")
    ap.add_argument("--no-switch", action="store_true", help="train and score, but don't switch the server to it")
    ap.add_argument("--revert", action="store_true", help="go back to the stock model")
    ap.add_argument("--status", action="store_true", help="show how many training clips there are and exit")
    args = ap.parse_args()

    if args.revert:
        if os.path.exists(ACTIVE):
            os.remove(ACTIVE)
        say("Back to the stock model. Restart the server.")
        return
    clips = load_clips()
    train, test = split(clips)
    say(f"Training clips with audio: {len(clips)}  (learn from {len(train)}, test on {len(test)})")
    if args.status:
        say(f"Server model now: {os.path.relpath(current_model(), HERE)}")
        return
    if len(clips) < args.min_clips or len(test) < 5:
        say(f"Not enough yet: need {args.min_clips}+ corrected or confirmed clips (use ✓ / Fix on the dashboard). "
            f"More is better; 300+ gives the most reliable results.")
        sys.exit(1)

    try:
        with open(os.path.join(HERE, "config.json")) as f:
            prompt = json.load(f).get("whisper_prompt", "")
    except (OSError, ValueError):
        prompt = ""
    cur = args.current or current_model()
    say(f"\n1/4  Scoring the current model ({os.path.basename(cur)}) on {len(test)} held-back clips...")
    before, rows_before = eval_mlx(cur, test, prompt)
    say(f"     word error rate: {100 * before:.1f}%")

    say("\n2/4  Fine-tuning...")
    model = finetune(args.hf_base, train, args)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = os.path.join(MODELS, f"finetuned-{stamp}-mlx")
    say(f"\n3/4  Converting for the Mac GPU -> {os.path.relpath(out, HERE)}")
    hf_to_mlx(model, out)
    del model

    say("\n4/4  Scoring the new model on the same held-back clips...")
    after, rows_after = eval_mlx(out, test, prompt)
    say(f"     word error rate: {100 * after:.1f}%   (was {100 * before:.1f}%)")

    better = after < before * 0.97
    report = {"when": stamp, "clips": len(clips), "train": len(train), "test": len(test),
              "wer_before": round(before, 4), "wer_after": round(after, 4), "switched": better and not args.no_switch,
              "compared_against": os.path.relpath(cur, HERE), "args": vars(args),
              "examples": [{"ref": a["ref"], "before": a["hyp"], "after": b["hyp"]} for a, b in zip(rows_before, rows_after)]}
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    changed = [e for e in report["examples"] if e["before"] != e["after"]][:8]
    if changed:
        say("\nSome held-back clips, before -> after:")
        for e in changed:
            say(f"  said:   {e['ref']}\n  before: {e['before']}\n  after:  {e['after']}\n")
    try:
        with open(os.path.join(TRAIN_DIR, "runs.jsonl"), "a") as f:
            f.write(json.dumps({k: v for k, v in report.items() if k != "examples"}) + "\n")
    except OSError:
        pass
    if better and not args.no_switch:
        with open(ACTIVE, "w") as f:
            f.write(os.path.relpath(out, HERE))
        say(f"Better by {100 * (before - after) / max(before, 1e-9):.0f}%: switched on. Restart the server to use it.")
    elif better:
        say("Better, but not switched on (--no-switch).")
    else:
        say("Not clearly better, so the server keeps its current model. Collect more corrections and try again.")
        if not args.no_switch:
            shutil.rmtree(out, ignore_errors=True)        # don't pile up 800 MB models that weren't used


if __name__ == "__main__":
    main()
