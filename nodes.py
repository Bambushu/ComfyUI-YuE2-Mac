"""YuE2 song generation on Apple Silicon through yue2.cpp (Metal).

The nodes run the yue-synth / yue-plan binaries in a subprocess: a crash cannot take ComfyUI down,
and every byte of GPU memory is returned when the render ends.
"""
import json, os, queue, re, subprocess, threading, uuid, wave

import numpy as np
import comfy.model_management as mm
import comfy.utils
import folder_paths
from comfy_extras.nodes_audio import load as load_audio

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
HF_REPO = "Serveurperso/YuE2-GGUF"
DEFAULT_MODEL = "YuE2-3B-Q8_0.gguf"
DEFAULT_VAE = "YuE2-Vae-F32.gguf"

DEFAULT_TRANSCRIBER = "SheetSage2-Q8_0.gguf"
DEFAULT_TOKENIZER = "yue2-mothersuperior-realaudio-tokenizer-v4-Q8_0.gguf"
ADAPTER_REPO = "Mothersuperior/yue2-mothersuperior-realaudio-tokenizer-v4"
REALAUDIO_ADAPTER = "nar_lora_joint_v9.safetensors"

MODEL_DIR = os.path.join(folder_paths.models_dir, "yue2")
ADAPTER_DIR = os.path.join(MODEL_DIR, "adapters")
os.makedirs(ADAPTER_DIR, exist_ok=True)
folder_paths.folder_names_and_paths["yue2"] = ([MODEL_DIR], {".gguf"})


def _adapters():
    found = sorted(f for f in os.listdir(ADAPTER_DIR) if f.endswith(".safetensors"))
    return ["none"] + (found if REALAUDIO_ADAPTER in found else found + [REALAUDIO_ADAPTER])


def _mert_for(path):
    """Transcriber and tokenizer load MERT from their own folder, in the matching precision."""
    name = "MERT-v2-FullSong-" + ("F32" if "F32" in os.path.basename(path) else "Q8_0") + ".gguf"
    if not os.path.exists(os.path.join(os.path.dirname(path), name)):
        _model_path(name)
    return path


def _save_wav(audio, path):
    """ComfyUI AUDIO -> 16-bit WAV for the yue2.cpp tools."""
    w = audio["waveform"][0].float().clamp(-1, 1).cpu().numpy()  # [C, T]
    with wave.open(path, "wb") as f:
        f.setnchannels(w.shape[0])
        f.setsampwidth(2)
        f.setframerate(audio["sample_rate"])
        f.writeframes((w.T * 32767).astype(np.int16).tobytes())


def _codes(text):
    return ",".join(t for t in re.split(r"[,\s]+", text or "") if t)


def _choices(prefix, default):
    found = [f for f in folder_paths.get_filename_list("yue2") if os.path.basename(f).startswith(prefix)]
    return found or [default]


def _model_path(name):
    path = folder_paths.get_full_path("yue2", name)
    if path:
        return path
    from huggingface_hub import hf_hub_download
    print(f"[YuE2] downloading {name} from {HF_REPO}")
    return hf_hub_download(HF_REPO, name, local_dir=MODEL_DIR)


def _adapter_path(name):
    path = os.path.join(ADAPTER_DIR, name)
    if not os.path.exists(path) and name == REALAUDIO_ADAPTER:
        from huggingface_hub import hf_hub_download
        print(f"[YuE2] downloading {name} from {ADAPTER_REPO}")
        hf_hub_download(ADAPTER_REPO, name, local_dir=ADAPTER_DIR)
    return path


def _binary(name):
    path = os.path.join(BIN, name)
    if not os.path.exists(path):
        raise RuntimeError(f"[YuE2] {path} missing. Run: bash {os.path.join(HERE, 'build.sh')}")
    return path


# Progress milestones parsed from the binaries' log, in percent.
STAGES = [
    (re.compile(r"\[AR\] Score: "), lambda m: 10),
    (re.compile(r"\[AR\] Semantic: "), lambda m: 35),
    (re.compile(r"\[NAR\] Step (\d+)/(\d+)"), lambda m: 35 + 60 * int(m.group(1)) // int(m.group(2))),
    (re.compile(r"Tiled decode done"), lambda m: 98),
]


def _run(cmd, done_at=100):
    """Run a yue2.cpp binary with progress, interrupt and a readable error."""
    mm.unload_all_models()
    mm.soft_empty_cache(True)
    pbar = comfy.utils.ProgressBar(100)
    lines, q = [], queue.Queue()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    threading.Thread(target=lambda: [q.put(l) for l in p.stdout] + [q.put(None)], daemon=True).start()
    while True:
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            if mm.processing_interrupted():
                p.terminate()
                p.wait()
                mm.throw_exception_if_processing_interrupted()
            continue
        if line is None:
            break
        lines.append(line.rstrip())
        for rx, pct in STAGES:
            m = rx.search(line)
            if m:
                pbar.update_absolute(min(pct(m), done_at))
    if p.wait() != 0:
        raise RuntimeError("[YuE2] failed:\n" + "\n".join(lines[-20:]))
    pbar.update_absolute(100)
    return lines


def _request(song, score="", codes="", source_codes="", source_start=0.0, source_end=-1.0, adapter="none", strength=1.0):
    req = {k: song[k] for k in ("style", "lyrics", "cot", "duration")}
    req["abc"] = score.strip()
    req["output_format"] = "wav32"
    if _codes(codes) and _codes(source_codes):
        raise ValueError("[YuE2] connect codes (re-render) or source_codes (extend), not both")
    if _codes(codes):
        req["semantic_tokens"] = _codes(codes)
    if _codes(source_codes):
        req.update(source_tokens=_codes(source_codes), source_start=source_start, source_end=source_end)
    if adapter != "none":
        req["adapters"] = [{"name": adapter, "scale": strength}]
    return req


class YuE2Song:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "style": ("STRING", {"multiline": True, "default": "English, upbeat funk jingle, groovy female vocal, slap bass, wah guitar, tight drums, horn stabs, 110 BPM"}),
            "lyrics": ("STRING", {"multiline": True, "default": "[Verse]\n...\n\n[Chorus]\n..."}),
            "cot": (["full", "melody", "off"], {"tooltip": "full: chord-annotated score, melody: melody-only score, off: no score stage"}),
            "duration": ("FLOAT", {"default": 360.0, "min": 10.0, "max": 600.0, "step": 5.0, "tooltip": "Maximum length in seconds; the model can end the song earlier."}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF}),
        }}

    RETURN_TYPES = ("YUE2_SONG",)
    FUNCTION = "build"
    CATEGORY = "audio/YuE2"

    def build(self, style, lyrics, cot, duration, seed):
        return ({"style": style, "lyrics": lyrics, "cot": cot, "duration": duration, "seed": seed},)


class YuE2Plan:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "song": ("YUE2_SONG",),
            "model": (_choices("YuE2-3B", DEFAULT_MODEL),),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("score",)
    FUNCTION = "plan"
    CATEGORY = "audio/YuE2"
    DESCRIPTION = "Writes the ABC score only (fast). Edit it and feed it to YuE2 Render to keep the melody."

    def plan(self, song, model):
        tmp = os.path.join(folder_paths.get_temp_directory(), f"yue2_{uuid.uuid4().hex}")
        with open(tmp + ".json", "w") as f:
            json.dump(_request(song), f)
        _run([_binary("yue-plan"), "--model", _model_path(model), "--request", tmp + ".json",
              "--lm-seed", str(song["seed"]), "--out", tmp + ".abc"], done_at=95)
        with open(tmp + ".abc") as f:
            return (f.read(),)


class YuE2Render:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "song": ("YUE2_SONG",),
                "model": (_choices("YuE2-3B", DEFAULT_MODEL),),
                "vae": (_choices("YuE2-Vae", DEFAULT_VAE),),
                "steps": ("INT", {"default": 32, "min": 4, "max": 100, "tooltip": "Flow matching steps of the acoustic stage."}),
            },
            "optional": {
                "score": ("STRING", {"forceInput": True, "tooltip": "ABC score from YuE2 Plan or YuE2 Transcribe (cover). Empty: the model writes its own."}),
                "codes": ("STRING", {"forceInput": True, "tooltip": "Codes from YuE2 Tokenize: re-render that recording through YuE2 (use the realaudio adapter)."}),
                "source_codes": ("STRING", {"forceInput": True, "tooltip": "Codes of a source song: the new song starts with the section below and grows from it."}),
                "source_start": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 600.0, "step": 0.5, "tooltip": "Section start in the source, seconds."}),
                "source_end": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 600.0, "step": 0.5, "tooltip": "Section end in the source, seconds; -1 = end of the source."}),
                "adapter": (_adapters(), {"tooltip": f"LoRA from models/yue2/adapters. {REALAUDIO_ADAPTER} pairs with YuE2 Tokenize codes (downloads on first use)."}),
                "adapter_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05}),
            },
        }

    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "score")
    FUNCTION = "render"
    CATEGORY = "audio/YuE2"

    def render(self, song, model, vae, steps, score="", codes="", source_codes="", source_start=0.0, source_end=-1.0,
               adapter="none", adapter_strength=1.0):
        tmp = os.path.join(folder_paths.get_temp_directory(), f"yue2_{uuid.uuid4().hex}")
        with open(tmp + ".json", "w") as f:
            json.dump(_request(song, score or "", codes, source_codes, source_start, source_end, adapter, adapter_strength), f)
        seed = str(song["seed"])
        cmd = [_binary("yue-synth"), "--model", _model_path(model), "--vae", _model_path(vae),
               "--request", tmp + ".json", "--lm-seed", seed, "--seed", seed, "--steps", str(steps),
               "--score", tmp + ".abc", "--out", tmp + ".wav"]
        if adapter != "none":
            _adapter_path(adapter)
            cmd += ["--adapters", ADAPTER_DIR]
        _run(cmd)
        waveform, sr = load_audio(tmp + ".wav")
        used = open(tmp + ".abc").read() if os.path.exists(tmp + ".abc") else (score or "")
        return ({"waveform": waveform.unsqueeze(0), "sample_rate": sr}, used)


class YuE2Transcribe:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "audio": ("AUDIO",),
            "model": (_choices("SheetSage2", DEFAULT_TRANSCRIBER),),
            "melody_only": ("BOOLEAN", {"default": True, "tooltip": "On for covers (pair with cot = melody). Off keeps the chord symbols."}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("score",)
    FUNCTION = "transcribe"
    CATEGORY = "audio/YuE2"
    DESCRIPTION = "SheetSage2: the ABC score of a recording. Feed it to YuE2 Render's score input for a cover."

    def transcribe(self, audio, model, melody_only):
        tmp = os.path.join(folder_paths.get_temp_directory(), f"yue2_{uuid.uuid4().hex}")
        _save_wav(audio, tmp + ".wav")
        cmd = [_binary("yue-transcribe"), "--model", _mert_for(_model_path(model)), "--audio", tmp + ".wav", "--out", tmp + ".abc"]
        _run(cmd + (["--melody-only"] if melody_only else []))
        with open(tmp + ".abc") as f:
            return (f.read(),)


class YuE2Tokenize:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "audio": ("AUDIO",),
            "model": (_choices("yue2-mothersuperior", DEFAULT_TOKENIZER),),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("codes",)
    FUNCTION = "tokenize"
    CATEGORY = "audio/YuE2"
    DESCRIPTION = "Semantic codes of a recording (25 per second), for YuE2 Render's codes (re-render) or source_codes (extend)."

    def tokenize(self, audio, model):
        tmp = os.path.join(folder_paths.get_temp_directory(), f"yue2_{uuid.uuid4().hex}")
        _save_wav(audio, tmp + ".wav")
        _run([_binary("yue-tokenize"), "--model", _mert_for(_model_path(model)), "--audio", tmp + ".wav", "--out", tmp + ".csv"])
        with open(tmp + ".csv") as f:
            return (_codes(f.read()),)


NODE_CLASS_MAPPINGS = {"YuE2Song": YuE2Song, "YuE2Plan": YuE2Plan, "YuE2Render": YuE2Render,
                       "YuE2Transcribe": YuE2Transcribe, "YuE2Tokenize": YuE2Tokenize}
NODE_DISPLAY_NAME_MAPPINGS = {"YuE2Song": "YuE2 Song", "YuE2Plan": "YuE2 Plan (score only)", "YuE2Render": "YuE2 Render",
                              "YuE2Transcribe": "YuE2 Transcribe (audio to score)", "YuE2Tokenize": "YuE2 Tokenize (audio to codes)"}
