#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Surface Pro 6 Smart Pen Chinese & English Handwriting + Next-Word Prediction Daemon
1. Single compact writing box (420x270) supporting both Chinese (米字格) and English (四线三格).
2. Bilingual Handwriting:
   - Default "🌐 中/EN" mode recognizes both Chinese characters and English words/letters/numbers.
   - Toggling "🇬🇧 EN" mode switches to dedicated English word/cursive recognition with auto-spacing.
3. Automatic Next-Input Prediction (自动预测下一个输入):
   - Immediately after committing a Chinese character/word or English word, the 8 candidate buttons
     display predicted next characters/words (powered by fcitx5 libIMECore zh_CN.lm.predict N-gram
     model + 42,376-entry libime prefix dictionary + English N-gram bigram predictor).
   - Tapping a predicted token commits it immediately and chains to predict the following token!
4. Uses POPUP (override-redirect) window + evdev stylus grab over the pad so writing and
   tapping candidates NEVER steals focus from the target input box.
"""

import ctypes
import errno
import http.client
import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

# Force X11 backend, disable GTK IM module, and disable AT-SPI bridge so our popup
# never registers with fcitx5 or triggers GNOME Shell's buggy libatk-bridge-2.0
os.environ["GDK_BACKEND"] = "x11"
os.environ["GTK_IM_MODULE"] = "gtk-im-context-simple"
os.environ["NO_AT_BRIDGE"] = "1"

import cairo
import evdev
from evdev import UInput, ecodes
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = b"/usr/share/tegaki/models/zinnia/handwriting-zh_CN.model"
LIBIME_BRIDGE_PATH = os.path.join(BASE_DIR, "libime_predict_bridge.so")
LIBIME_LM_PATH = b"/usr/lib/x86_64-linux-gnu/libime/zh_CN.lm"
ZH_PREFIX_DICT_PATH = os.path.join(BASE_DIR, "zh_prefix_dict.tsv")
ZH_PINYIN_DICT_PATH = os.path.join(BASE_DIR, "zh_pinyin_dict.tsv")

STATE_FILE = f"/run/user/{os.getuid()}/surface_hw_im_state.txt"
VISIBLE_FLAG_FILE = f"/run/user/{os.getuid()}/surface_pen_handwriting_visible"
PID_FILE = f"/run/user/{os.getuid()}/surface_pen_handwriting.pid"
DEFAULT_PUNCTUATIONS_ZH = ["，", "。", "？", "！", "、", "：", "；", "↻"]
DEFAULT_PUNCTUATIONS_EN = [",", ".", "?", "!", "'", "\"", ":", "↻"]

PUNCTUATION_PAGES_ZH = [
    ["，", "。", "？", "！", "、", "：", "；", "↻"],
    ["“", "”", "（", "）", "《", "》", "…", "↻"],
    ["【", "】", "‘", "’", "—", "～", "·", "↻"],
    ["@", "#", "¥", "%", "+", "-", "=", "↻"],
]

PUNCTUATION_PAGES_EN = [
    [",", ".", "?", "!", "'", "\"", ":", "↻"],
    ["(", ")", "[", "]", "-", "_", "/", "↻"],
    ["@", "#", "$", "%", "&", "*", "+", "↻"],
    ["=", "<", ">", "~", "|", "\\", ";", "↻"],
]

ALL_PUNCTUATIONS_SET = {
    "，", "。", "？", "！", "、", "：", "；", "“", "”", "‘", "’",
    "（", "）", "《", "》", "【", "】", "…", "—", "～", "·",
    ",", ".", "?", "!", "'", "\"", ":", ";", "(", ")", "[", "]",
    "{", "}", "-", "_", "/", "\\", "@", "#", "$", "¥", "%", "&",
    "*", "+", "=", "<", ">", "~", "|",
}

# Auto-commit delay (milliseconds) after lifting pen
AUTO_COMMIT_MS = 950

# High-frequency conversational Chinese character/word starters to prioritize top completions
ZH_PRIORITY_NEXT = {
    "你": ["好", "们", "的", "是", "在", "可以", "知道", "怎么"],
    "你好": ["吗", "呀", "！", "，", "啊", "朋友"],
    "我": ["们", "的", "是", "在", "觉得", "喜欢", "想", "要"],
    "我们": ["的", "是", "在", "可以", "一起", "要", "都"],
    "今": ["天", "年", "日", "晚", "后", "生", "早"],
    "今天": ["天气", "的", "是", "在", "上午", "下午", "晚上", "我们"],
    "明": ["天", "白", "年", "显", "确", "日", "星"],
    "明天": ["见", "的", "上午", "下午", "天气", "是", "去"],
    "手": ["写", "机", "动", "段", "术", "表", "里"],
    "手写": ["输入", "板", "笔", "识别", "的", "功能"],
    "智": ["能", "慧", "力", "商", "能手机", "能化"],
    "人": ["工", "们", "员", "民", "生", "口", "才"],
    "人工": ["智能", "服务", "成本", "客服", "合成"],
    "欢": ["迎", "乐", "喜", "快", "呼"],
    "欢迎": ["光临", "大家", "来到", "你", "访问"],
    "谢": ["谢", "谢你", "谢大家", "绝", "幕"],
    "谢谢": ["你", "你们", "大家", "您", "！"],
    "可": ["以", "能", "是", "爱", "靠", "行"],
    "可以": ["吗", "的", "在", "用", "帮", "看", "试试"],
    "什": ["么", "么时候", "么样", "么事"],
    "什么": ["时候", "样", "事", "是", "意思", "都", "？"],
    "怎": ["么", "样", "么办", "么样", "奈"],
    "怎么": ["样", "办", "回事", "了", "做", "用", "？"],
    "现": ["在", "场", "代", "实", "象", "金"],
    "现在": ["的", "是", "在", "可以", "就", "开始"],
    "电": ["脑", "话", "池", "影", "视", "子", "源"],
    "中": ["国", "文", "心", "间", "午", "的"],
    "英": ["语", "文", "国", "雄", "寸"],
}

# High-frequency English conversational next-word bigrams
EN_NEXT_WORDS = {
    "i": ["am", "have", "will", "think", "want", "need", "can", "would"],
    "im": ["going", "here", "ready", "not", "sorry", "happy", "working"],
    "i'm": ["going", "here", "ready", "not", "sorry", "happy", "working"],
    "you": ["are", "can", "have", "need", "want", "know", "will", "should"],
    "we": ["are", "can", "have", "will", "need", "should", "want"],
    "they": ["are", "have", "will", "can", "were", "want"],
    "he": ["is", "was", "has", "will", "can", "said"],
    "she": ["is", "was", "has", "will", "can", "said"],
    "it": ["is", "was", "will", "can", "works", "has", "looks"],
    "hello": ["world", "there", "everyone", "how", "friend", "again"],
    "hi": ["there", "everyone", "how", "all", "guys"],
    "how": ["are", "to", "much", "many", "about", "can", "do", "is"],
    "what": ["is", "are", "do", "about", "time", "if", "can", "happened"],
    "where": ["is", "are", "to", "can", "do", "did"],
    "when": ["is", "will", "you", "we", "can", "it"],
    "why": ["is", "not", "do", "are", "did", "would"],
    "who": ["is", "are", "can", "will", "was"],
    "thank": ["you", "goodness", "god", "him", "them"],
    "thanks": ["for", "a", "to", "so", "very", "again"],
    "please": ["help", "check", "let", "see", "make", "send", "wait", "do"],
    "can": ["you", "i", "we", "be", "not", "help", "do", "see"],
    "could": ["you", "be", "not", "have", "we", "please"],
    "would": ["you", "like", "be", "love", "have", "not"],
    "should": ["be", "have", "we", "i", "not"],
    "will": ["be", "have", "you", "not", "do", "make"],
    "good": ["morning", "afternoon", "evening", "night", "job", "idea", "luck"],
    "nice": ["to", "work", "day", "job", "one", "meeting"],
    "surface": ["pro", "pen", "linux", "book", "go", "laptop"],
    "linux": ["kernel", "system", "ubuntu", "desktop", "driver"],
    "this": ["is", "will", "can", "works", "looks", "one", "way"],
    "that": ["is", "was", "would", "can", "will", "looks"],
    "there": ["is", "are", "was", "were", "will"],
    "have": ["a", "to", "been", "you", "fun", "good", "time"],
    "need": ["to", "a", "help", "more", "some", "it"],
    "want": ["to", "a", "it", "more", "you"],
    "like": ["to", "this", "it", "that", "a"],
    "look": ["at", "for", "good", "like", "into"],
    "let": ["me", "us", "it", "them", "him"],
    "let's": ["go", "see", "try", "start", "check", "do"],
    "lets": ["go", "see", "try", "start", "check", "do"],
    "are": ["you", "we", "they", "not", "going", "ready", "here"],
    "is": ["it", "this", "that", "not", "a", "the", "good", "working"],
}


def is_cjk_str(s: str) -> bool:
    return bool(s) and all("\u4e00" <= ch <= "\u9fff" for ch in s)


def is_ascii_word(s: str) -> bool:
    return bool(s) and s.isascii() and any(ch.isalnum() for ch in s)


def read_im_state():
    """Return (is_active: bool, program: str, mtime: float) from fcitx5 state file."""
    try:
        st = os.stat(STATE_FILE)
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines()]
        state = lines[0] if lines else ""
        prog = lines[1] if len(lines) > 1 else ""
        return (state.startswith("ACTIVE"), prog, st.st_mtime)
    except OSError:
        return (False, "", 0.0)


def force_im_state_active():
    """Restore ACTIVE in STATE_FILE so touching the pad or typing on keyboard never marks the input box INACTIVE."""
    try:
        _, prog, _ = read_im_state()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(f"ACTIVE\n{prog}\n")
    except OSError:
        pass


def set_pad_visible_flag(visible: bool):
    try:
        with open(VISIBLE_FLAG_FILE, "w", encoding="utf-8") as f:
            f.write("1\n" if visible else "0\n")
    except OSError:
        pass


def get_strokes_bbox(strokes):
    pts = [pt for s in strokes for pt in s]
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)
    return (min_x, min_y, max(1.0, max_x - min_x), max(1.0, max_y - min_y))


def normalize_strokes(strokes, target_size: int = 1000, margin_ratio: float = 0.12):
    """Normalize strokes into a centered [0..target_size] square preserving aspect ratio."""
    pts = [pt for s in strokes for pt in s]
    if not pts:
        return []
    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)

    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    span = max(span_x, span_y, 24.0)

    usable = target_size * (1.0 - 2.0 * margin_ratio)
    scale = usable / span
    cx = (min_x + max_x) * 0.5
    cy = (min_y + max_y) * 0.5
    half = target_size * 0.5

    normed = []
    for s in strokes:
        ns = []
        for pt in s:
            x, y = pt[0], pt[1]
            nx = int(round(half + (x - cx) * scale))
            ny = int(round(half + (y - cy) * scale))
            nx = max(0, min(target_size, nx))
            ny = max(0, min(target_size, ny))
            if not ns or ns[-1] != (nx, ny):
                ns.append((nx, ny))
        if ns:
            normed.append(ns)
    return normed


def normalize_strokes_rect(strokes, target_w: int = 270, target_h: int = 195, margin_ratio: float = 0.10):
    """
    Normalize strokes into a rectangular [0..target_w] x [0..target_h] box preserving true aspect ratio.
    Critical so wide multi-letter English words ("hello", "surface") or multi-character Chinese words are never squashed.
    """
    pts = [pt for s in strokes for pt in s]
    if not pts:
        return []
    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)

    span_x = max(18.0, max_x - min_x)
    span_y = max(18.0, max_y - min_y)

    usable_w = target_w * (1.0 - 2.0 * margin_ratio)
    usable_h = target_h * (1.0 - 2.0 * margin_ratio)
    scale = min(usable_w / span_x, usable_h / span_y)

    cx = (min_x + max_x) * 0.5
    cy = (min_y + max_y) * 0.5
    half_w = target_w * 0.5
    half_h = target_h * 0.5

    normed = []
    for s in strokes:
        ns = []
        for pt in s:
            x, y = pt[0], pt[1]
            nx = int(round(half_w + (x - cx) * scale))
            ny = int(round(half_h + (y - cy) * scale))
            nx = max(0, min(target_w, nx))
            ny = max(0, min(target_h, ny))
            if not ns or ns[-1] != (nx, ny):
                ns.append((nx, ny))
        if ns:
            normed.append(ns)
    return normed


class PinyinEngine:
    """
    Hybrid 0ms Offline (119,018-entry libIMEPinyin + zh_CN.lm dictionary) + ~80ms Cloud Pinyin IME
    for typing Chinese on the built-in Virtual Keyboard.
    Returns list of (candidate_word, matched_pinyin_length).
    """

    def __init__(self):
        self.pinyin_map = {}
        self._load_dict()

    def _load_dict(self):
        if os.path.exists(ZH_PINYIN_DICT_PATH):
            try:
                with open(ZH_PINYIN_DICT_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if "\t" not in line:
                            continue
                        k, vals = line.split("\t", 1)
                        if k and vals:
                            self.pinyin_map[k] = vals.split(",")
            except OSError:
                pass

    def lookup_sync(self, py_buf: str):
        """Return up to 8 (word, matched_len) tuples synchronously in 0ms."""
        buf = py_buf.strip().lower()
        if not buf:
            return []
        out = []
        seen = set()

        def add(word: str, mlen: int):
            if word and word not in seen:
                seen.add(word)
                out.append((word, mlen))

        # 1. Exact full-buffer match (e.g., "nihao" -> "你好", "jt" -> "今天")
        for w in self.pinyin_map.get(buf, []):
            add(w, len(buf))
            if len(out) >= 6:
                break

        # 2. Longest prefix match (e.g., "nihaoshijie" -> "你好" (len=5), "你" (len=2))
        for plen in range(len(buf) - 1, 0, -1):
            sub = buf[:plen]
            if sub in self.pinyin_map:
                for w in self.pinyin_map[sub][:4]:
                    add(w, plen)
                    if len(out) >= 7:
                        break
            if len(out) >= 7:
                break

        # 3. Always include raw English buffer as the last candidate
        add(buf, len(buf))
        return out[:8]

    def fetch_cloud(self, py_buf: str):
        """Query Google Pinyin IME for full-sentence candidates with matched_length."""
        buf = py_buf.strip().lower()
        if not buf or not buf.isalpha():
            return []
        url = (
            "https://inputtools.google.com/request?text="
            + urllib.parse.quote(buf)
            + "&itc=zh-t-i0-pinyin&num=8&cp=0&cs=1&ie=utf-8&oe=utf-8"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=1.2) as r:
                data = json.loads(r.read().decode("utf-8"))
                if data and data[0] == "SUCCESS" and data[1]:
                    entry = data[1][0]
                    cands = entry[1] if len(entry) > 1 else []
                    meta = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
                    mlens = meta.get("matched_length", [])
                    res = []
                    for i, c in enumerate(cands):
                        ml = mlens[i] if i < len(mlens) else len(buf)
                        res.append((c, int(ml)))
                    return res
        except Exception:
            pass
        return []


class NextTokenPredictor:
    """
    Fast hybrid offline + online Next-Word / Next-Character Predictor for Chinese & English:
    1. Chinese Prefix Dictionary (zh_prefix_dict.tsv, 42,376 entries scored by zh_CN.lm)
    2. fcitx5 libIMECore N-gram Sentence Prediction (libime_predict_bridge.so + zh_CN.lm.predict)
    3. English High-Frequency Conversational Bigrams + Datamuse N-gram Cloud Follower Lookup
    """

    def __init__(self):
        self.zh_prefix_map = {}
        self.libime_lib = None
        self.libime_handle = None
        self._load_zh_resources()

    def _load_zh_resources(self):
        if os.path.exists(ZH_PREFIX_DICT_PATH):
            try:
                with open(ZH_PREFIX_DICT_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if "\t" not in line:
                            continue
                        k, vals = line.split("\t", 1)
                        if k and vals:
                            self.zh_prefix_map[k] = vals.split(",")
            except OSError:
                pass

        if os.path.exists(LIBIME_BRIDGE_PATH) and os.path.exists(LIBIME_LM_PATH):
            try:
                self.libime_lib = ctypes.CDLL(LIBIME_BRIDGE_PATH)
                self.libime_lib.predictor_new.argtypes = [ctypes.c_char_p]
                self.libime_lib.predictor_new.restype = ctypes.c_void_p
                self.libime_lib.predictor_predict.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_char_p,
                    ctypes.c_int,
                ]
                self.libime_lib.predictor_predict.restype = ctypes.c_char_p
                self.libime_handle = self.libime_lib.predictor_new(LIBIME_LM_PATH)
            except Exception:
                self.libime_lib = None
                self.libime_handle = None

    def _libime_ngram_predict(self, tokens, max_results: int = 12):
        if not self.libime_lib or not self.libime_handle or not tokens:
            return []
        try:
            joined = "|".join(tokens[-3:]).encode("utf-8")
            raw = self.libime_lib.predictor_predict(self.libime_handle, joined, max_results)
            if not raw:
                return []
            text = raw.decode("utf-8", errors="ignore")
            return [w for w in text.split("\t") if w]
        except Exception:
            return []

    def predict_sync(self, token_history, last_token: str, completion_hints=None, lang_mode: str = "zh-CN"):
        """Return up to 8 instant 0ms predicted next tokens."""
        if not last_token:
            return []

        if is_ascii_word(last_token) or lang_mode == "en":
            return self._predict_english_sync(last_token, completion_hints or [])
        return self._predict_chinese_sync(token_history, last_token, completion_hints or [])

    def _predict_chinese_sync(self, token_history, last_token: str, completion_hints):
        out = []
        seen = set()

        def add(tok: str):
            tok = tok.strip()
            if tok and tok not in seen and tok != last_token:
                seen.add(tok)
                out.append(tok)

        # 1. Multi-char suffix hints from Google Handwriting recognition
        for hint in completion_hints:
            add(hint)

        # 2. Combined 2-char context (e.g. wrote "今" then tapped "天" -> check "今天")
        combined_2 = ""
        if len(token_history) >= 2 and len(token_history[-2]) == 1 and len(last_token) == 1:
            combined_2 = token_history[-2] + last_token
            for w in ZH_PRIORITY_NEXT.get(combined_2, []):
                add(w)
            for w in self.zh_prefix_map.get(combined_2, [])[:4]:
                add(w)

        # 3. Priority conversational completions for last_token
        for w in ZH_PRIORITY_NEXT.get(last_token, []):
            add(w)

        # 4. libime 42,376-entry prefix completion dictionary (top 5)
        for w in self.zh_prefix_map.get(last_token, [])[:5]:
            add(w)

        # 5. fcitx5 libIMECore N-gram language model prediction
        query_tokens = list(token_history[-3:])
        if combined_2:
            for w in self._libime_ngram_predict([combined_2], 10):
                if w not in ("。", "，", "、", "！", "？"):
                    add(w)
        for w in self._libime_ngram_predict(query_tokens, 12):
            if w not in ("。", "，", "、", "！", "？"):
                add(w)

        # 6. Remaining prefix dictionary items if still under 7
        for w in self.zh_prefix_map.get(last_token, []):
            if len(out) >= 7:
                break
            add(w)

        # Always keep "，" or "。" in the 8th slot for quick sentence punctuation
        out = out[:7]
        if "，" not in seen:
            out.append("，")
        elif "。" not in seen:
            out.append("。")
        return out[:8]

    def _predict_english_sync(self, last_token: str, completion_hints):
        out = []
        seen = set()
        key = last_token.strip().lower()

        def add(tok: str):
            tok = tok.strip()
            if tok and tok.lower() not in seen and tok.lower() != key:
                seen.add(tok.lower())
                out.append(tok)

        # 1. Built-in high-frequency conversational next words
        for w in EN_NEXT_WORDS.get(key, []):
            add(w)

        # 2. Completion hints from handwriting recognizer
        for h in completion_hints:
            add(h)

        # 3. High-utility English connectors & punctuation fallback
        for fallback in ["the", "to", "and", "is", "in", "for", "of", ","]:
            if len(out) >= 8:
                break
            add(fallback)

        return out[:8]

    def fetch_english_cloud_next(self, last_token: str):
        """Query Datamuse Google Books N-gram follower API (rel_bga) for English next-word prediction."""
        key = last_token.strip().lower()
        if not key or not key.isalpha():
            return []
        url = f"https://api.datamuse.com/words?rel_bga={urllib.parse.quote(key)}&max=10"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=1.2) as r:
                data = json.loads(r.read().decode("utf-8"))
                return [
                    d["word"]
                    for d in data
                    if isinstance(d.get("word"), str) and d["word"].isalpha() and len(d["word"]) >= 2
                ]
        except Exception:
            return []


class ZinniaRecognizer:
    """Offline Simplified Chinese handwriting recognizer with bounding-box normalization."""

    def __init__(self, model_path: bytes = MODEL_PATH):
        self.lib = ctypes.CDLL("libzinnia.so.0")
        self.lib.zinnia_recognizer_new.restype = ctypes.c_void_p
        self.lib.zinnia_recognizer_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.zinnia_recognizer_open.restype = ctypes.c_int
        self.lib.zinnia_recognizer_classify.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.lib.zinnia_recognizer_classify.restype = ctypes.c_void_p
        self.lib.zinnia_character_new.restype = ctypes.c_void_p
        self.lib.zinnia_character_clear.argtypes = [ctypes.c_void_p]
        self.lib.zinnia_character_set_width.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.zinnia_character_set_height.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.zinnia_character_add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.lib.zinnia_result_size.argtypes = [ctypes.c_void_p]
        self.lib.zinnia_result_size.restype = ctypes.c_size_t
        self.lib.zinnia_result_value.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.zinnia_result_value.restype = ctypes.c_char_p
        self.lib.zinnia_result_destroy.argtypes = [ctypes.c_void_p]

        self.rec = self.lib.zinnia_recognizer_new()
        if not self.lib.zinnia_recognizer_open(self.rec, model_path):
            raise RuntimeError(f"Failed to open Zinnia model: {model_path.decode()}")
        self.ch = self.lib.zinnia_character_new()

    def classify(self, strokes, max_candidates: int = 10):
        if not strokes:
            return []
        normed = normalize_strokes(strokes, target_size=1000, margin_ratio=0.12)
        self.lib.zinnia_character_clear(self.ch)
        self.lib.zinnia_character_set_width(self.ch, 1000)
        self.lib.zinnia_character_set_height(self.ch, 1000)
        for idx, stroke in enumerate(normed):
            for x, y in stroke:
                self.lib.zinnia_character_add(self.ch, idx, int(x), int(y))

        res = self.lib.zinnia_recognizer_classify(self.rec, self.ch, max_candidates)
        if not res:
            return []
        try:
            n = self.lib.zinnia_result_size(res)
            out = []
            for i in range(n):
                val = self.lib.zinnia_result_value(res, i)
                if val:
                    s = val.decode("utf-8", errors="ignore").strip()
                    if is_cjk_str(s):
                        out.append(s)
            return out
        finally:
            self.lib.zinnia_result_destroy(res)


class GoogleNeuralRecognizer:
    """
    Stroke-order-free, cursive-tolerant neural Chinese & English handwriting recognizer
    using persistent HTTPS keep-alive connection (~90ms latency).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None

    def _get_conn(self):
        if self._conn is None:
            self._conn = http.client.HTTPSConnection("inputtools.google.com", timeout=1.5)
        return self._conn

    def _request_lang(self, ink, guide_w: int, guide_h: int, lang: str, pre_context: str, max_results: int):
        payload = json.dumps(
            {
                "app_version": 0.4,
                "api_level": "537.36",
                "device": "5.0",
                "input_type": 0,
                "options": "enable_pre_space",
                "requests": [
                    {
                        "writing_guide": {
                            "writing_area_width": guide_w,
                            "writing_area_height": guide_h,
                        },
                        "pre_context": pre_context[-6:] if pre_context else "",
                        "max_num_results": max_results,
                        "max_completions": 8 if lang == "en" else 4,
                        "language": lang,
                        "ink": ink,
                    }
                ],
            }
        )
        headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
        with self._lock:
            for _ in range(2):
                try:
                    conn = self._get_conn()
                    conn.request(
                        "POST",
                        "/request?ime=handwriting&app=mobilesearch&cs=1&oe=UTF-8",
                        body=payload,
                        headers=headers,
                    )
                    resp = conn.getresponse()
                    raw_bytes = resp.read()
                    if resp.status != 200:
                        self._conn = None
                        continue
                    data = json.loads(raw_bytes.decode("utf-8"))
                    if data and data[0] == "SUCCESS" and data[1]:
                        return data[1][0][1]
                except Exception:
                    try:
                        if self._conn:
                            self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
        return []

    def classify(
        self,
        strokes,
        width: int,
        height: int,
        pre_context: str = "",
        lang_mode: str = "zh-CN",
        max_results: int = 16,
        wide_mode: bool = False,
    ):
        """
        Return (candidates: list[str], completion_hints: dict[str, list[str]]).
        Strictly separates Chinese mode ("zh-CN" -> CJK only) and English mode ("en" -> ASCII only)
        so Chinese and English handwriting never confuse each other.
        Automatically detects wide multi-character continuous writing.
        """
        if not strokes:
            return [], {}

        if lang_mode == "en":
            guide_w, guide_h = max(270, width), max(195, height)
            normed = normalize_strokes_rect(strokes, target_w=guide_w, target_h=guide_h, margin_ratio=0.10)
            ink = [[[p[0] for p in s], [p[1] for p in s]] for s in normed]
            raw_cands = self._request_lang(ink, guide_w, guide_h, "en", pre_context, max_results)
            return self._filter_candidates(raw_cands, lang_mode="en")

        pts = [pt for s in strokes for pt in s]
        span_x = (max(p[0] for p in pts) - min(p[0] for p in pts)) if pts else 0.0
        span_y = max(18.0, (max(p[1] for p in pts) - min(p[1] for p in pts)) if pts else 18.0)
        is_multi_char = wide_mode or (span_x > span_y * 1.45 and len(strokes) >= 2)

        if is_multi_char:
            guide_w, guide_h = max(380, width), max(195, height)
            normed = normalize_strokes_rect(strokes, target_w=guide_w, target_h=guide_h, margin_ratio=0.08)
            ink = [[[p[0] for p in s], [p[1] for p in s]] for s in normed]
            raw_zh = self._request_lang(ink, guide_w, guide_h, "zh-CN", pre_context, max_results)
            return self._filter_candidates(raw_zh, lang_mode="zh-CN", prefer_multi=True)

        # Single-character Chinese mode ("zh-CN"): use square normalization optimal for CJK characters
        guide_w, guide_h = 250, 250
        normed = normalize_strokes(strokes, target_size=250, margin_ratio=0.10)
        ink = [[[p[0] for p in s], [p[1] for p in s]] for s in normed]
        raw_zh = self._request_lang(ink, guide_w, guide_h, "zh-CN", pre_context, max_results)
        return self._filter_candidates(raw_zh, lang_mode="zh-CN", prefer_multi=False)

    @staticmethod
    def _filter_candidates(raw_cands, lang_mode: str = "zh-CN", prefer_multi: bool = False):
        """
        In "zh-CN" mode: strictly return CJK characters/words only (no ASCII letters mixed in).
        In "en" mode: strictly return English ASCII letters/words only (no CJK mixed in).
        """
        cleaned = []
        seen = set()
        for c in raw_cands:
            c = c.strip()
            if not c or c in seen:
                continue
            if len(c) >= 2 and is_cjk_str(c) and len(set(c)) == 1 and c not in (
                "谢谢", "看看", "试试", "妈妈", "爸爸", "哥哥", "姐姐", "弟弟", "妹妹", "哈哈", "呵呵", "天天", "常常", "刚刚", "慢慢"
            ):
                continue
            seen.add(c)
            cleaned.append(c)

        if lang_mode == "en":
            en_only = [c for c in cleaned if is_ascii_word(c)]
            return en_only, {}

        # Extract suffix completion hints for Chinese NextTokenPredictor
        completion_hints = {}
        for base in cleaned:
            if len(base) == 1 and is_cjk_str(base):
                hints = []
                for other in cleaned:
                    if len(other) >= 2 and other.startswith(base) and is_cjk_str(other):
                        suf = other[len(base) :]
                        if suf and suf not in hints:
                            hints.append(suf)
                if hints:
                    completion_hints[base] = hints

        single_cjk = [c for c in cleaned if len(c) == 1 and is_cjk_str(c)]
        multi_cjk = [c for c in cleaned if len(c) >= 2 and is_cjk_str(c)]
        if prefer_multi:
            return multi_cjk + single_cjk, completion_hints
        return single_cjk + multi_cjk, completion_hints


class VirtualTyper:
    """
    Commits text and special keys to any Wayland or XWayland window.
    Registers full key range (1..248) so udev/libinput sets ID_INPUT_KEYBOARD=1
    and GNOME Mutter accepts all keystrokes from this virtual keyboard.
    """

    def __init__(self):
        cap = {ecodes.EV_KEY: list(range(1, 249))}
        self.ui = UInput(cap, name="Surface-Pen-Handwriting-Virtual-Kbd")
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)

    @staticmethod
    def _is_terminal_active() -> bool:
        _, prog, _ = read_im_state()
        p = (prog or "").lower()
        return any(k in p for k in ("terminal", "konsole", "kitty", "alacritty", "wezterm"))

    def tap_key(self, keycode: int):
        self.ui.write(ecodes.EV_KEY, keycode, 1)
        self.ui.syn()

        def release_key():
            self.ui.write(ecodes.EV_KEY, keycode, 0)
            self.ui.syn()
            return False

        GLib.timeout_add(15, release_key)

    def commit_text(self, text: str):
        if not text:
            return
        self.clipboard.set_text(text, -1)
        self.primary.set_text(text, -1)

        def send_paste():
            use_shift = self._is_terminal_active()
            self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
            if use_shift:
                self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
            self.ui.write(ecodes.EV_KEY, ecodes.KEY_V, 1)
            self.ui.syn()

            def release_paste():
                self.ui.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
                if use_shift:
                    self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                self.ui.syn()
                return False

            GLib.timeout_add(18, release_paste)
            return False

        GLib.timeout_add(30, send_paste)


class WritingCanvas(Gtk.EventBox):
    """Compact handwriting canvas supporting both Chinese 米字格 and English 四线三格 guides."""

    def __init__(self, on_stroke_start_cb, on_strokes_changed_cb):
        super().__init__()
        self.on_stroke_start_cb = on_stroke_start_cb
        self.on_strokes_changed_cb = on_strokes_changed_cb
        self.strokes = []
        self.current_stroke = None
        self.hover_pt = None
        self.last_release_time = 0.0
        self.lang_mode = "zh-CN"
        self.wide_mode = False
        self.canvas_w = 270
        self.canvas_h = 195

        self.image = Gtk.Image()
        self.add(self.image)
        self.set_size_request(self.canvas_w, self.canvas_h)
        self.connect("size-allocate", self._on_size_allocate)
        self._render_canvas()

    def set_lang_mode(self, lang_mode: str):
        if self.lang_mode != lang_mode:
            self.lang_mode = lang_mode
            self._render_canvas()

    def set_wide_mode(self, wide_mode: bool):
        if self.wide_mode != wide_mode:
            self.wide_mode = wide_mode
            self._render_canvas()

    def _on_size_allocate(self, widget, alloc):
        if alloc.width > 10 and alloc.height > 10:
            if alloc.width != self.canvas_w or alloc.height != self.canvas_h:
                self.canvas_w = alloc.width
                self.canvas_h = alloc.height
                self._render_canvas()

    def clear(self):
        self.strokes.clear()
        self.current_stroke = None
        self._render_canvas()

    def undo_stroke(self):
        if self.strokes:
            self.strokes.pop()
            self._render_canvas()
            self.on_strokes_changed_cb()

    def has_strokes(self) -> bool:
        return bool(self.strokes)

    def is_drawing(self) -> bool:
        return self.current_stroke is not None

    def set_hover(self, pt):
        if self.hover_pt != pt:
            self.hover_pt = pt
            self._render_canvas()

    def start_stroke(self, x: float, y: float, pressure: float = 0.5):
        now = time.monotonic()
        p = max(0.08, min(1.0, pressure))
        self.on_stroke_start_cb()
        if self.strokes and (now - self.last_release_time) < 0.065:
            last_pt = self.strokes[-1][-1]
            lx, ly = last_pt[0], last_pt[1]
            if math.hypot(x - lx, y - ly) < 14.0:
                self.current_stroke = self.strokes.pop()
                self.current_stroke.append((x, y, p))
                self._render_canvas()
                return
        self.current_stroke = [(x, y, p)]
        self._render_canvas()

    def move_stroke(self, x: float, y: float, pressure: float = 0.5):
        if self.current_stroke is not None:
            last_pt = self.current_stroke[-1]
            last_x, last_y, last_p = last_pt[0], last_pt[1], (last_pt[2] if len(last_pt) > 2 else 0.5)
            if (x - last_x) ** 2 + (y - last_y) ** 2 >= 2.2:
                # Smooth pressure transition
                p = 0.4 * last_p + 0.6 * max(0.08, min(1.0, pressure))
                self.current_stroke.append((x, y, p))
                self._render_canvas()

    def end_stroke(self):
        if self.current_stroke is not None:
            self.last_release_time = time.monotonic()
            if len(self.current_stroke) == 1:
                pt0 = self.current_stroke[0]
                x, y, p = pt0[0], pt0[1], (pt0[2] if len(pt0) > 2 else 0.5)
                self.current_stroke.append((x + 1, y + 1, p))
            self.strokes.append(self.current_stroke)
            self.current_stroke = None
            self._render_canvas()
            self.on_strokes_changed_cb()

    def _render_canvas(self):
        w, h = self.canvas_w, self.canvas_h
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)

        # Dark slate background (BGRA order in memory)
        cr.set_source_rgb(0.18, 0.15, 0.13)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        if self.lang_mode == "en":
            # English 4-line baseline guides (四线三格)
            cr.set_source_rgba(0.55, 0.45, 0.35, 0.42)
            cr.set_line_width(1.0)
            for frac, dashed in [(0.24, True), (0.45, True), (0.70, False), (0.88, True)]:
                cr.set_dash([5.0, 5.0] if dashed else [])
                y_line = h * frac
                cr.move_to(8, y_line)
                cr.line_to(w - 8, y_line)
                cr.stroke()
        elif self.wide_mode:
            # Dual Rice-Grid (双米字格连写) guide lines for multi-character continuous Chinese writing
            cr.set_source_rgba(0.52, 0.42, 0.35, 0.38)
            cr.set_line_width(1.0)
            cr.set_dash([5.0, 5.0])
            mid_x = w * 0.5
            cr.move_to(0, h * 0.5)
            cr.line_to(w, h * 0.5)
            for box_x0, box_x1 in ((0.0, mid_x), (mid_x, float(w))):
                bx_mid = (box_x0 + box_x1) * 0.5
                cr.move_to(bx_mid, 0)
                cr.line_to(bx_mid, h)
                cr.move_to(box_x0, 0)
                cr.line_to(box_x1, h)
                cr.move_to(box_x1, 0)
                cr.line_to(box_x0, h)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(0.70, 0.52, 0.38, 0.45)
            cr.move_to(mid_x, 4)
            cr.line_to(mid_x, h - 4)
            cr.stroke()
        else:
            # Subtle Rice-Grid (米字格) guide lines for Chinese single-character mode
            cr.set_source_rgba(0.52, 0.42, 0.35, 0.38)
            cr.set_line_width(1.0)
            cr.set_dash([5.0, 5.0])
            cr.move_to(0, h / 2)
            cr.line_to(w, h / 2)
            cr.move_to(w / 2, 0)
            cr.line_to(w / 2, h)
            cr.move_to(0, 0)
            cr.line_to(w, h)
            cr.move_to(w, 0)
            cr.line_to(0, h)
            cr.stroke()

        # Subtle watermark badge in bottom-right of canvas
        cr.set_dash([])
        cr.set_source_rgba(0.82, 0.55, 0.38, 0.65)
        cr.set_line_width(2.0)
        cr.rectangle(1, 1, w - 2, h - 2)
        cr.stroke()

        # Draw 4096-level pressure-sensitive ink strokes
        cr.set_source_rgb(1.0, 0.98, 0.95)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        all_strokes = list(self.strokes)
        if self.current_stroke:
            all_strokes.append(self.current_stroke)

        for stroke in all_strokes:
            if not stroke:
                continue
            for i in range(1, len(stroke)):
                p0 = stroke[i - 1]
                p1 = stroke[i]
                pr0 = p0[2] if len(p0) > 2 else 0.5
                pr1 = p1[2] if len(p1) > 2 else 0.5
                width_px = 2.4 + 5.2 * ((pr0 + pr1) * 0.5)
                cr.set_line_width(width_px)
                cr.move_to(p0[0], p0[1])
                cr.line_to(p1[0], p1[1])
                cr.stroke()

        # Hover indicator dot
        if self.hover_pt is not None:
            hx, hy = self.hover_pt
            cr.set_source_rgba(0.95, 0.65, 0.25, 0.85)
            cr.arc(hx, hy, 3.5, 0, 2 * math.pi)
            cr.fill()

        surf.flush()
        pixbuf = GdkPixbuf.Pixbuf.new_from_data(
            bytes(surf.get_data()),
            GdkPixbuf.Colorspace.RGB,
            True,
            8,
            w,
            h,
            surf.get_stride(),
        )
        self.image.set_from_pixbuf(pixbuf)


class SurfaceHandwritingWindow(Gtk.Window):
    """
    Compact single-box Override-Redirect (POPUP) floating handwriting pad.
    Supports Chinese & English handwriting and automatic next-input prediction.
    """

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_title("Surface Pen 手写板")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_app_paintable(True)
        screen = self.get_screen()
        rgba_visual = screen.get_rgba_visual() if screen else None
        if rgba_visual is not None:
            self.set_visual(rgba_visual)
        self.stick()

        self.zinnia = ZinniaRecognizer()
        self.google = GoogleNeuralRecognizer()
        self.predictor = NextTokenPredictor()
        self.pinyin = PinyinEngine()
        self.typer = VirtualTyper()

        self.lang_mode = "zh-CN"  # "zh-CN" (中文) or "en" (英文)
        self.wide_hw_mode = False
        self.punct_page_idx = 0
        self.candidates = list(DEFAULT_PUNCTUATIONS_ZH)
        self.completion_hints = {}
        self.is_prediction_bar = False
        self.pinyin_buf = ""
        self.pinyin_match_lens = []
        self._pinyin_seq = 0
        self.pre_context = ""
        self.token_history = []
        self.last_committed_token = ""
        self._rec_seq = 0
        self._auto_commit_timer_id = None
        self.osk_mode = False
        self.pen_battery = self._load_cached_pen_battery()

        self.win_x = 450
        self.win_y = 480
        self.win_w = 420
        self.win_h = 270

        self._is_drawing_canvas = False
        self._drag_active = False
        self._drag_offset_x = 0.0
        self._drag_offset_y = 0.0
        self._bksp_delay_timer_id = None
        self._bksp_repeat_timer_id = None

        self._apply_css()
        self._build_ui()

    def _load_cached_pen_battery(self) -> str:
        cache_path = os.path.expanduser("~/.cache/surface_pen_battery.txt")
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    val = f.read().strip()
                    if val:
                        return val
        except OSError:
            pass
        return "52%"

    def refresh_pen_battery_async(self):
        def _query():
            try:
                out = subprocess.check_output(
                    ["upower", "-d"], text=True, stderr=subprocess.DEVNULL, timeout=2.0
                )
                in_pen = False
                for line in out.splitlines():
                    if "Device:" in line:
                        in_pen = "bluetooth_generic_dev_" in line
                    elif in_pen and "model:" in line and "Surface Pen" in line:
                        in_pen = True
                    elif in_pen and "percentage:" in line:
                        pct = line.split(":", 1)[1].strip().split()[0]
                        if pct and pct != "0%":
                            cache_path = os.path.expanduser("~/.cache/surface_pen_battery.txt")
                            try:
                                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                                with open(cache_path, "w", encoding="utf-8") as f:
                                    f.write(pct)
                            except OSError:
                                pass
                            self.pen_battery = pct
                            break
            except Exception:
                pass

        threading.Thread(target=_query, daemon=True).start()

    def _apply_css(self):
        css = b"""
        window {
            background-color: rgba(0, 0, 0, 0);
            border: none;
        }
        .main-frame {
            background-color: #181b20;
            border: 2px solid #3b82f6;
            border-radius: 14px;
            padding: 5px 6px 6px 6px;
        }
        .header-bar {
            background-color: #1f242d;
            padding: 3px 8px;
            border-radius: 9px;
            border-bottom: 1px solid #2e3440;
        }
        .header-title {
            color: #e5e9f0;
            font-weight: bold;
            font-size: 12px;
        }
        .cand-btn {
            background-image: none;
            background-color: #252b35;
            color: #f8fafc;
            font-size: 17px;
            font-weight: 600;
            border: 1px solid #374151;
            border-radius: 7px;
            padding: 4px 4px;
            min-width: 42px;
            min-height: 40px;
        }
        .cand-btn-primary {
            background-image: none;
            background-color: #2563eb;
            color: #ffffff;
            font-size: 18px;
            font-weight: bold;
            border: 1px solid #60a5fa;
            border-radius: 7px;
            padding: 4px 6px;
            min-width: 46px;
            min-height: 40px;
        }
        .cand-btn-predict {
            background-image: none;
            background-color: #133836;
            color: #6ee7b7;
            font-size: 16px;
            font-weight: 600;
            border: 1px solid #059669;
            border-radius: 7px;
            padding: 4px 4px;
            min-width: 42px;
            min-height: 40px;
        }
        .action-btn {
            background-image: none;
            background-color: #262c36;
            color: #e2e8f0;
            font-size: 13px;
            font-weight: 600;
            border: 1px solid #3b4252;
            border-radius: 7px;
            padding: 4px 8px;
            min-height: 34px;
        }
        .lang-btn-active {
            background-image: none;
            background-color: #1d4ed8;
            color: #ffffff;
            font-size: 12px;
            font-weight: bold;
            border: 1px solid #60a5fa;
            border-radius: 7px;
            padding: 3px 8px;
            min-height: 30px;
        }
        .vkbd-btn {
            background-image: none;
            background-color: #252b35;
            color: #f8fafc;
            font-size: 15px;
            font-weight: 600;
            border: 1px solid #374151;
            border-radius: 6px;
            padding: 2px 2px;
            min-width: 32px;
            min-height: 34px;
        }
        .btn-flash {
            background-image: none;
            background-color: #3b82f6;
            color: #ffffff;
            border: 1px solid #93c5fd;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        root.get_style_context().add_class("main-frame")
        self.add(root)

        # 1. Compact Draggable Header Bar with Language Mode Toggle
        self.header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.header_box.get_style_context().add_class("header-bar")

        self.title_label = Gtk.Label(label="✒ 中文手写 (写完点选候选字上屏)")
        self.title_label.get_style_context().add_class("header-title")
        self.header_box.pack_start(self.title_label, False, False, 2)

        self.lang_btn = Gtk.Button(label="🇨🇳 中文")
        self.lang_btn.get_style_context().add_class("action-btn")

        self.wide_btn = Gtk.Button(label="⇄ 连写")
        self.wide_btn.get_style_context().add_class("action-btn")

        self.sym_btn = Gtk.Button(label="※ 符号")
        self.sym_btn.get_style_context().add_class("action-btn")

        self.osk_btn = Gtk.Button(label="⌨ 键盘")
        self.osk_btn.get_style_context().add_class("action-btn")

        self.hide_btn = Gtk.Button(label="✕")
        self.hide_btn.get_style_context().add_class("action-btn")

        self.header_box.pack_end(self.hide_btn, False, False, 0)
        self.header_box.pack_end(self.osk_btn, False, False, 1)
        self.header_box.pack_end(self.sym_btn, False, False, 1)
        self.header_box.pack_end(self.wide_btn, False, False, 1)
        self.header_box.pack_end(self.lang_btn, False, False, 1)
        root.pack_start(self.header_box, False, False, 0)

        # 2. Candidate / Next-Token Prediction Bar (8 buttons)
        cand_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.cand_buttons = []
        for i in range(8):
            btn = Gtk.Button(label=DEFAULT_PUNCTUATIONS_ZH[i])
            btn.get_style_context().add_class("cand-btn")
            cand_row.pack_start(btn, True, True, 0)
            self.cand_buttons.append(btn)
        root.pack_start(cand_row, False, False, 1)

        # 3. Main Area Stack: Page "hw" (Handwriting Canvas), Page "kbd" (Wide Full Keyboard), Page "sym" (Symbols)
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.main_stack.set_homogeneous(False)

        # Page A: Single Canvas + Action Column
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.canvas = WritingCanvas(self._on_stroke_start, self._on_strokes_changed)
        body.pack_start(self.canvas, True, True, 0)

        ctrl_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        ctrl_col.set_size_request(96, -1)

        self.btn_undo = Gtk.Button(label="↶ 退一笔")
        self.btn_clear = Gtk.Button(label="🗑 重写")
        self.btn_bksp = Gtk.Button(label="⌫ 退格")
        self.btn_space = Gtk.Button(label="␣ 空格")
        self.btn_enter = Gtk.Button(label="↵ 回车")

        for b in (self.btn_undo, self.btn_clear, self.btn_bksp, self.btn_space, self.btn_enter):
            b.get_style_context().add_class("action-btn")
            ctrl_col.pack_start(b, True, True, 0)

        body.pack_start(ctrl_col, False, False, 0)
        self.main_stack.add_named(body, "hw")

        # Page B: Wide Rectangular Full-Key Virtual Keyboard (780x285 total window)
        kbd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        kbd_box.set_size_request(760, 200)
        self.vkbd_shift = False
        self.vkbd_caps = False
        self.vkbd_shift_btn = None
        self.vkbd_caps_btn = None
        self.vkbd_bksp_btn = None
        self.vkbd_buttons = []  # list of (btn, base_key)
        self.vkbd_shift_map = {
            "`": "~", "1": "!", "2": "@", "3": "#", "4": "$",
            "5": "%", "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
            "-": "_", "=": "+", "[": "{", "]": "}", "\\": "|",
            ";": ":", "'": "\"", ",": "<", ".": ">", "/": "?",
        }
        rows = [
            ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=", "⌫ 退格"],
            ["Tab", "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "[", "]", "\\"],
            ["Caps", "a", "s", "d", "f", "g", "h", "j", "k", "l", ";", "'", "↵ 回车"],
            ["⇧ Shift", "z", "x", "c", "v", "b", "n", "m", ",", ".", "/", "↑"],
            ["※ 符号", "@", "␣ 空格 (Space)", "，", "。", "←", "↓", "→"],
        ]
        for r in rows:
            rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for key_lbl in r:
                btn = Gtk.Button(label=key_lbl)
                btn.get_style_context().add_class("vkbd-btn")
                if key_lbl == "␣ 空格 (Space)":
                    btn.set_size_request(260, -1)
                    expand = True
                elif key_lbl in ("⌫ 退格", "↵ 回车", "⇧ Shift", "Caps", "Tab", "※ 符号"):
                    btn.set_size_request(68, -1)
                    expand = True
                else:
                    expand = True
                rbox.pack_start(btn, expand, True, 0)
                self.vkbd_buttons.append((btn, key_lbl))
                if key_lbl == "⇧ Shift":
                    self.vkbd_shift_btn = btn
                elif key_lbl == "Caps":
                    self.vkbd_caps_btn = btn
                elif key_lbl == "⌫ 退格":
                    self.vkbd_bksp_btn = btn
            kbd_box.pack_start(rbox, True, True, 0)
        self.main_stack.add_named(kbd_box, "kbd")

        # Page C: Full Categorized Punctuation & Symbol Panel (50 symbols, wide layout)
        sym_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sym_box.set_size_request(600, 195)
        self.sym_bksp_btn = None
        self.sym_buttons = []  # list of (btn, sym_str)
        sym_rows = [
            ["，", "。", "？", "！", "、", "：", "；", "…", "—", "～"],
            ["“", "”", "‘", "’", "（", "）", "《", "》", "【", "】"],
            [",", ".", "?", "!", ":", ";", "\"", "'", "(", ")"],
            ["@", "#", "¥", "$", "%", "&", "*", "+", "-", "="],
            ["/", "\\", "_", "|", "<", ">", "[", "]", "~", "⌫"],
        ]
        for sr in sym_rows:
            sbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for s_lbl in sr:
                sbtn = Gtk.Button(label=s_lbl)
                sbtn.get_style_context().add_class("vkbd-btn")
                sbox.pack_start(sbtn, True, True, 0)
                self.sym_buttons.append((sbtn, s_lbl))
                if s_lbl == "⌫":
                    self.sym_bksp_btn = sbtn
            sym_box.pack_start(sbox, True, True, 0)
        self.main_stack.add_named(sym_box, "sym")

        root.pack_start(self.main_stack, True, True, 0)

    def _default_punctuations(self, reset_page: bool = True):
        if reset_page:
            self.punct_page_idx = 0
        pages = PUNCTUATION_PAGES_EN if self.lang_mode == "en" else PUNCTUATION_PAGES_ZH
        return list(pages[self.punct_page_idx % len(pages)])

    def _flash_button(self, btn):
        if btn is None:
            return
        ctx = btn.get_style_context()
        ctx.add_class("btn-flash")

        def remove_flash():
            ctx.remove_class("btn-flash")
            return False

        GLib.timeout_add(130, remove_flash)

    def _clear_pinyin_buf(self):
        self.pinyin_buf = ""
        self.pinyin_match_lens = []
        self._pinyin_seq += 1

    def _update_pinyin_candidates(self):
        if not self.pinyin_buf:
            self.pinyin_match_lens = []
            self._update_candidates(self._default_punctuations(), mode="punctuation")
            return

        buf_snap = self.pinyin_buf
        local_pairs = self.pinyin.lookup_sync(buf_snap)
        self.pinyin_match_lens = [ml for _, ml in local_pairs]
        self._update_candidates([w for w, _ in local_pairs], mode="pinyin")

        self._pinyin_seq += 1
        seq = self._pinyin_seq

        def run_cloud_py():
            cloud_pairs = self.pinyin.fetch_cloud(buf_snap)
            if not cloud_pairs:
                return
            merged = []
            seen = set()
            for w, ml in cloud_pairs + local_pairs:
                if w and w not in seen:
                    seen.add(w)
                    merged.append((w, ml))
                    if len(merged) >= 7:
                        break
            if buf_snap not in seen:
                merged.append((buf_snap, len(buf_snap)))
            else:
                for w, ml in local_pairs:
                    if w not in seen and len(merged) < 8:
                        seen.add(w)
                        merged.append((w, ml))

            def apply_py():
                if self._pinyin_seq == seq and self.pinyin_buf == buf_snap:
                    top8 = merged[:8]
                    self.pinyin_match_lens = [ml for _, ml in top8]
                    self._update_candidates([w for w, _ in top8], mode="pinyin")
                return False

            GLib.idle_add(apply_py)

        threading.Thread(target=run_cloud_py, daemon=True).start()

    def _toggle_lang_mode(self):
        self._cancel_auto_commit()
        self._clear_pinyin_buf()
        if self.lang_mode == "zh-CN":
            self.lang_mode = "en"
            self.lang_btn.set_label("🇬🇧 英文")
            self.lang_btn.get_style_context().remove_class("action-btn")
            self.lang_btn.get_style_context().add_class("lang-btn-active")
        else:
            self.lang_mode = "zh-CN"
            self.lang_btn.set_label("🇨🇳 中文")
            self.lang_btn.get_style_context().remove_class("lang-btn-active")
            self.lang_btn.get_style_context().add_class("action-btn")

        self.canvas.set_lang_mode(self.lang_mode)
        if self.canvas.has_strokes():
            self._on_strokes_changed()
        else:
            self._update_candidates(self._default_punctuations(), mode="punctuation")

    def _toggle_wide_hw_mode(self):
        self._cancel_auto_commit()
        self.wide_hw_mode = not self.wide_hw_mode
        self.canvas.set_wide_mode(self.wide_hw_mode)
        ctx = self.wide_btn.get_style_context()
        if self.wide_hw_mode:
            self.wide_btn.set_label("▣ 单格")
            ctx.remove_class("action-btn")
            ctx.add_class("lang-btn-active")
        else:
            self.wide_btn.set_label("⇄ 连写")
            ctx.remove_class("lang-btn-active")
            ctx.add_class("action-btn")

        if self.main_stack.get_visible_child_name() != "hw":
            self.main_stack.set_visible_child_name("hw")
            self.osk_btn.set_label("⌨ 键盘")
            self.sym_btn.set_label("※ 符号")
        self._resize_for_page("hw")
        if self.canvas.has_strokes():
            self._on_strokes_changed()
        else:
            self._update_candidates(self._default_punctuations(reset_page=False), mode="punctuation")

    @staticmethod
    def get_screen_size():
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        geom = mon.get_geometry()
        return geom.width, geom.height

    def position_avoiding_point(self, tap_x: float, tap_y: float):
        """
        Position the compact single-box handwriting pad well away from (tap_x, tap_y)
        so the entire input box and the text being typed are 100% visible.
        """
        sw, sh = self.get_screen_size()
        w, h = self.win_w, self.win_h

        x = int(tap_x - w * 0.35)
        x = max(16, min(sw - w - 16, x))

        if tap_y > sh * 0.45:
            y = max(28, int(tap_y) - h - 115)
        else:
            y = min(sh - h - 28, int(tap_y) + 95)

        self.win_x, self.win_y = x, y
        self.move(x, y)

    def contains_point(self, sx: float, sy: float, margin: float = 0.0) -> bool:
        if not self.get_visible():
            return False
        alloc = self.get_allocation()
        w = max(self.win_w, alloc.width if alloc.width > 100 else self.win_w)
        h = max(self.win_h, alloc.height if alloc.height > 100 else self.win_h)
        return (
            (self.win_x - margin) <= sx <= (self.win_x + w + margin)
            and (self.win_y - margin) <= sy <= (self.win_y + h + margin)
        )

    def _hit_widget(self, widget, sx: float, sy: float):
        res = widget.translate_coordinates(self, 0, 0)
        if not res:
            return False, 0.0, 0.0
        wx, wy = res
        alloc = widget.get_allocation()
        lx = sx - self.win_x - wx
        ly = sy - self.win_y - wy
        hit = 0 <= lx <= alloc.width and 0 <= ly <= alloc.height
        return hit, lx, ly

    def on_stylus_hover(self, sx: float, sy: float):
        if not self.get_visible():
            return
        hit, lx, ly = self._hit_widget(self.canvas, sx, sy)
        self.canvas.set_hover((lx, ly) if hit else None)

    def _stop_bksp_repeat(self):
        if self._bksp_delay_timer_id is not None:
            GLib.source_remove(self._bksp_delay_timer_id)
            self._bksp_delay_timer_id = None
        if self._bksp_repeat_timer_id is not None:
            GLib.source_remove(self._bksp_repeat_timer_id)
            self._bksp_repeat_timer_id = None

    def _start_bksp_press(self):
        self._stop_bksp_repeat()
        self._commit_or_key(ecodes.KEY_BACKSPACE)

        def begin_repeat():
            self._bksp_delay_timer_id = None
            if not self.get_visible():
                return False

            def repeat_step():
                if not self.get_visible():
                    self._bksp_repeat_timer_id = None
                    return False
                self._commit_or_key(ecodes.KEY_BACKSPACE)
                return True

            self._commit_or_key(ecodes.KEY_BACKSPACE)
            self._bksp_repeat_timer_id = GLib.timeout_add(65, repeat_step)
            return False

        self._bksp_delay_timer_id = GLib.timeout_add(350, begin_repeat)

    def _refresh_vkbd_labels(self):
        upper_letters = self.vkbd_shift ^ self.vkbd_caps
        for btn, kl in self.vkbd_buttons:
            if len(kl) == 1 and kl.isalpha():
                btn.set_label(kl.upper() if upper_letters else kl.lower())
            elif kl in self.vkbd_shift_map:
                btn.set_label(self.vkbd_shift_map[kl] if self.vkbd_shift else kl)
        if self.vkbd_shift_btn is not None:
            ctx = self.vkbd_shift_btn.get_style_context()
            if self.vkbd_shift:
                ctx.add_class("lang-btn-active")
            else:
                ctx.remove_class("lang-btn-active")
        if self.vkbd_caps_btn is not None:
            ctx = self.vkbd_caps_btn.get_style_context()
            if self.vkbd_caps:
                ctx.add_class("lang-btn-active")
            else:
                ctx.remove_class("lang-btn-active")

    def _on_vkbd_key(self, key_lbl: str):
        if key_lbl == "⇧ Shift":
            self.vkbd_shift = not self.vkbd_shift
            self._refresh_vkbd_labels()
            return
        if key_lbl == "Caps":
            self.vkbd_caps = not self.vkbd_caps
            self._refresh_vkbd_labels()
            return
        if key_lbl == "※ 符号":
            self._on_switch_to_sym(None)
            return
        if key_lbl in ("⌫", "⌫ 退格"):
            self._start_bksp_press()
            return
        if key_lbl in ("␣ 空格", "␣ 空格 (Space)"):
            self._commit_or_key(ecodes.KEY_SPACE)
            return
        if key_lbl == "↵ 回车":
            self._commit_or_key(ecodes.KEY_ENTER)
            return
        if key_lbl == "Tab":
            self._commit_or_key(ecodes.KEY_TAB)
            return
        if key_lbl == "↑":
            self.typer.tap_key(ecodes.KEY_UP)
            return
        if key_lbl == "↓":
            self.typer.tap_key(ecodes.KEY_DOWN)
            return
        if key_lbl == "←":
            self.typer.tap_key(ecodes.KEY_LEFT)
            return
        if key_lbl == "→":
            self.typer.tap_key(ecodes.KEY_RIGHT)
            return

        upper_letters = self.vkbd_shift ^ self.vkbd_caps
        # In Chinese mode ("zh-CN"), lowercase letters a..z enter Pinyin composition
        if self.lang_mode == "zh-CN" and not upper_letters and len(key_lbl) == 1 and key_lbl.isalpha():
            self.pinyin_buf += key_lbl.lower()
            self._update_pinyin_candidates()
            return

        # While composing Pinyin, tapping 1..8 selects candidate 1..8
        if self.pinyin_buf and not self.vkbd_shift and key_lbl in ("1", "2", "3", "4", "5", "6", "7", "8"):
            idx = int(key_lbl) - 1
            if idx < len(self.candidates):
                self._on_candidate_clicked(self.cand_buttons[idx], idx)
                return

        # If user types punctuation/symbols while Pinyin buffer has pending text, commit #1 candidate first
        if self.pinyin_buf and self.candidates:
            first_word = self.candidates[0]
            self._clear_pinyin_buf()
            self.typer.commit_text(first_word)

        if len(key_lbl) == 1 and key_lbl.isalpha():
            ch = key_lbl.upper() if upper_letters else key_lbl.lower()
        elif self.vkbd_shift and key_lbl in self.vkbd_shift_map:
            ch = self.vkbd_shift_map[key_lbl]
        else:
            ch = key_lbl

        if self.vkbd_shift:
            self.vkbd_shift = False
            self._refresh_vkbd_labels()
        self.typer.commit_text(ch)

    def on_stylus_down(self, sx: float, sy: float, pressure: float = 0.5, is_eraser: bool = False):
        if not self.get_visible():
            return
        self._cancel_auto_commit()
        self._stop_bksp_repeat()
        force_im_state_active()

        # Surface Pen Eraser (BTN_TOOL_RUBBER) or Barrel Side Button (BTN_STYLUS) quick action:
        if is_eraser:
            if self.canvas.has_strokes():
                self._flash_button(self.btn_undo)
                self._on_undo_clicked(None)
                self.title_label.set_text("🧹 笔尾橡皮擦/侧键：已退一笔 (空画布时点按可执行退格)")
            elif self.pinyin_buf:
                self._start_bksp_press()
            else:
                self._flash_button(self.btn_bksp)
                self._start_bksp_press()
            return

        # 1. Check writing canvas, virtual keyboard, or symbol panel depending on active stack page
        cur_page = self.main_stack.get_visible_child_name()
        if cur_page == "kbd":
            for btn, key_lbl in self.vkbd_buttons:
                hit, _, _ = self._hit_widget(btn, sx, sy)
                if hit:
                    self._flash_button(btn)
                    self._on_vkbd_key(key_lbl)
                    return
        elif cur_page == "sym":
            for btn, sym_lbl in self.sym_buttons:
                hit, _, _ = self._hit_widget(btn, sx, sy)
                if hit:
                    self._flash_button(btn)
                    if sym_lbl == "⌫":
                        self._start_bksp_press()
                    else:
                        self.typer.commit_text(sym_lbl)
                    return
        else:
            hit, lx, ly = self._hit_widget(self.canvas, sx, sy)
            if hit:
                self._is_drawing_canvas = True
                self.canvas.set_hover((lx, ly))
                self.canvas.start_stroke(lx, ly, pressure)
                return
            for btn, fn in [
                (self.btn_undo, lambda: self._on_undo_clicked(None)),
                (self.btn_clear, lambda: self._on_clear_clicked(None)),
                (self.btn_bksp, self._start_bksp_press),
                (self.btn_space, lambda: self._commit_or_key(ecodes.KEY_SPACE)),
                (self.btn_enter, lambda: self._commit_or_key(ecodes.KEY_ENTER)),
            ]:
                hit, _, _ = self._hit_widget(btn, sx, sy)
                if hit:
                    self._flash_button(btn)
                    fn()
                    return

        # 2. Check Candidate / Prediction buttons
        for i, btn in enumerate(self.cand_buttons):
            hit, _, _ = self._hit_widget(btn, sx, sy)
            if hit:
                self._flash_button(btn)
                self._on_candidate_clicked(btn, i)
                return

        # 3. Check Header buttons
        header_actions = [
            (self.lang_btn, self._toggle_lang_mode),
            (self.wide_btn, self._toggle_wide_hw_mode),
            (self.sym_btn, lambda: self._on_switch_to_sym(None)),
            (self.osk_btn, lambda: self._on_switch_to_osk(None)),
            (self.hide_btn, self.hide_all_panels),
        ]
        for btn, fn in header_actions:
            hit, _, _ = self._hit_widget(btn, sx, sy)
            if hit:
                self._flash_button(btn)
                fn()
                return

        # 4. Check Header Bar for dragging
        hit, _, _ = self._hit_widget(self.header_box, sx, sy)
        if hit:
            self._drag_active = True
            self._drag_offset_x = sx - self.win_x
            self._drag_offset_y = sy - self.win_y

    def on_stylus_motion(self, sx: float, sy: float, pressure: float = 0.5):
        if not self.get_visible():
            return
        if self._bksp_delay_timer_id is not None or self._bksp_repeat_timer_id is not None:
            cur_page = self.main_stack.get_visible_child_name()
            bksp_widget = (
                self.btn_bksp
                if cur_page == "hw"
                else (self.vkbd_bksp_btn if cur_page == "kbd" else self.sym_bksp_btn)
            )
            if bksp_widget is not None:
                hit_bksp, _, _ = self._hit_widget(bksp_widget, sx, sy)
                if not hit_bksp:
                    self._stop_bksp_repeat()
        if self._is_drawing_canvas:
            _, lx, ly = self._hit_widget(self.canvas, sx, sy)
            lx = max(0.0, min(float(self.canvas.canvas_w), lx))
            ly = max(0.0, min(float(self.canvas.canvas_h), ly))
            self.canvas.set_hover((lx, ly))
            self.canvas.move_stroke(lx, ly, pressure)
        elif self._drag_active:
            sw, sh = self.get_screen_size()
            nx = int(max(0, min(sw - self.win_w, sx - self._drag_offset_x)))
            ny = int(max(0, min(sh - self.win_h, sy - self._drag_offset_y)))
            self.win_x, self.win_y = nx, ny
            self.move(nx, ny)

    def on_stylus_up(self, sx: float, sy: float):
        self._stop_bksp_repeat()
        if self._is_drawing_canvas:
            self.canvas.end_stroke()
            self._is_drawing_canvas = False
        self._drag_active = False

    def _cancel_auto_commit(self):
        if self._auto_commit_timer_id is not None:
            GLib.source_remove(self._auto_commit_timer_id)
            self._auto_commit_timer_id = None

    def _schedule_auto_commit(self):
        self._cancel_auto_commit()

        def on_timeout():
            self._auto_commit_timer_id = None
            if self.canvas.is_drawing():
                return False
            if self.canvas.has_strokes() and self.candidates:
                self._rec_seq += 1
                committed = self.candidates[0]
                self.canvas.clear()
                self._commit_and_predict_next(committed)
            return False

        self._auto_commit_timer_id = GLib.timeout_add(AUTO_COMMIT_MS, on_timeout)

    def flush_pending_character(self):
        self._cancel_auto_commit()

    def _commit_and_predict_next(self, text: str):
        """
        Commit `text` to the active input box and immediately populate the 8 candidate buttons
        with predicted next characters/words (自动预测下一个输入).
        """
        if not text:
            return

        # Auto-insert space between consecutive multi-letter English words
        out_text = text
        if (
            is_ascii_word(self.last_committed_token)
            and is_ascii_word(text)
            and (self.lang_mode == "en" or len(self.last_committed_token) >= 2 or len(text) >= 2)
        ):
            out_text = " " + text

        self.last_committed_token = text
        self.pre_context = (self.pre_context + out_text)[-12:]
        if text not in ("，", "。", "？", "！", "、", ",", ".", "?", "!"):
            self.token_history.append(text)
            if len(self.token_history) > 8:
                self.token_history = self.token_history[-8:]
        else:
            self.token_history.clear()

        self.typer.commit_text(out_text)

        # If the user just committed punctuation, show default punctuation
        if text in ALL_PUNCTUATIONS_SET:
            self._update_candidates(self._default_punctuations(reset_page=False), mode="punctuation")
            return

        # 1. Instant 0ms next-token prediction
        hints = self.completion_hints.get(text, [])
        preds = self.predictor.predict_sync(
            self.token_history, text, completion_hints=hints, lang_mode=self.lang_mode
        )
        if preds:
            self._update_candidates(preds, mode="prediction")
        else:
            self._update_candidates(self._default_punctuations(), mode="punctuation")

        # 2. If committed token is an English word, asynchronously enrich with Google Books N-gram followers
        if is_ascii_word(text) and text.isalpha():
            self._rec_seq += 1
            seq = self._rec_seq

            def fetch_en_next():
                cloud_next = self.predictor.fetch_english_cloud_next(text)
                if not cloud_next:
                    return
                merged = []
                seen = set()
                # Keep top 3 local conversational bigrams first, then cloud N-grams
                for w in preds[:3] + cloud_next + preds[3:]:
                    lw = w.lower()
                    if lw not in seen and lw != text.lower():
                        seen.add(lw)
                        merged.append(w)

                def apply_en_pred():
                    if self._rec_seq == seq and not self.canvas.has_strokes() and not self.pinyin_buf:
                        self._update_candidates(merged[:8], mode="prediction")
                    return False

                GLib.idle_add(apply_en_pred)

            threading.Thread(target=fetch_en_next, daemon=True).start()

    def _on_stroke_start(self):
        self._cancel_auto_commit()

    def _on_strokes_changed(self):
        if not self.canvas.has_strokes():
            self._rec_seq += 1
            self._cancel_auto_commit()
            self._update_candidates(self._default_punctuations(), mode="punctuation")
            return

        strokes_snapshot = [list(s) for s in self.canvas.strokes]
        w, h = self.canvas.canvas_w, self.canvas.canvas_h

        # 1. Instant 0ms offline Zinnia recognition (only when in Chinese single-character mode)
        zinnia_cands = []
        if self.lang_mode != "en" and not self.wide_hw_mode:
            zinnia_cands = self.zinnia.classify(strokes_snapshot, 8)
            if zinnia_cands:
                self._update_candidates(zinnia_cands, mode="handwriting")

        # Do NOT auto-commit on a timer while the user is writing/choosing:
        # wait for the user to confirm by tapping a candidate button (or Space/Enter).

        # 2. Asynchronous ~90ms Google Neural Handwriting recognition (supports zh-CN, multi-char & en)
        self._rec_seq += 1
        seq = self._rec_seq
        pre_ctx = self.pre_context
        lang_mode = self.lang_mode
        wide_mode = self.wide_hw_mode

        def run_neural():
            neural_cands, comp_hints = self.google.classify(
                strokes_snapshot, w, h, pre_context=pre_ctx, lang_mode=lang_mode, max_results=16, wide_mode=wide_mode
            )
            if not neural_cands:
                return
            merged = list(neural_cands)
            for zc in zinnia_cands:
                if zc not in merged:
                    merged.append(zc)

            def apply_if_current():
                if self._rec_seq == seq and self.canvas.has_strokes():
                    self.completion_hints = comp_hints
                    self._update_candidates(merged[:8], mode="handwriting")
                return False

            GLib.idle_add(apply_if_current)

        threading.Thread(target=run_neural, daemon=True).start()

    def _update_candidates(self, cands, mode: str = "handwriting"):
        """
        mode:
          - "handwriting": active strokes on canvas (#1 highlighted in blue)
          - "pinyin": active pinyin composition on virtual keyboard (#1 highlighted in blue)
          - "prediction": canvas is empty, showing predicted next words/chars (emerald style)
          - "punctuation": canvas is empty, showing default punctuation
        """
        self.candidates = list(cands)
        self.is_prediction_bar = (mode == "prediction")

        cur_page = self.main_stack.get_visible_child_name()
        bat_suffix = f" · 🔋{self.pen_battery}" if self.pen_battery else ""
        if mode == "pinyin":
            self.title_label.set_text(
                f"⌨ 拼音 [{self.pinyin_buf}] (点选或1-8选词 · 空格选首词 · 回车输英文)"
            )
        elif mode == "prediction":
            self.title_label.set_text(
                f"💡 联想下一输入 (已上屏“{self.last_committed_token}” · 点选直接输入或继续书写)"
            )
        elif cur_page == "kbd":
            self.title_label.set_text(
                f"⌨ 英文全键盘 (直接打字{bat_suffix})"
                if self.lang_mode == "en"
                else f"⌨ 中文拼音全键盘 (敲字母出拼音候选 · 切换“🇬🇧 英文”直接打英文{bat_suffix})"
            )
        elif cur_page == "sym":
            self.title_label.set_text("※ 标点符号大全 (点选直接输入 · 点击“✒ 手写”返回)")
        elif self.lang_mode == "en":
            self.title_label.set_text(f"✒ 英文手写 (支持连写单词 · 点选候选词上屏{bat_suffix})")
        elif self.wide_hw_mode:
            self.title_label.set_text(f"✒ 中文多字连写 (可横写2-4字词组 · 点选候选词上屏{bat_suffix})")
        else:
            self.title_label.set_text(f"✒ 中文手写 (4096级压感 · 笔尾/侧键退一笔{bat_suffix})")

        for i, btn in enumerate(self.cand_buttons):
            ctx = btn.get_style_context()
            ctx.remove_class("cand-btn-primary")
            ctx.remove_class("cand-btn-predict")
            ctx.remove_class("cand-btn")
            if i < len(cands):
                btn.set_label(cands[i])
                btn.set_sensitive(True)
                if mode in ("handwriting", "pinyin") and i == 0:
                    ctx.add_class("cand-btn-primary")
                elif mode == "prediction":
                    ctx.add_class("cand-btn-predict")
                else:
                    ctx.add_class("cand-btn")
            else:
                btn.set_label("")
                btn.set_sensitive(False)
                ctx.add_class("cand-btn")

    def _on_candidate_clicked(self, button, idx: int):
        self._cancel_auto_commit()
        if idx < len(self.candidates):
            text = self.candidates[idx]
            if text == "↻":
                self.punct_page_idx += 1
                self._update_candidates(self._default_punctuations(reset_page=False), mode="punctuation")
                return

            # Handle Pinyin candidate selection (supports partial syllable consumption!)
            if self.pinyin_buf:
                mlen = (
                    self.pinyin_match_lens[idx]
                    if idx < len(self.pinyin_match_lens)
                    else len(self.pinyin_buf)
                )
                rem = self.pinyin_buf[max(1, mlen) :]
                if rem and text != self.pinyin_buf:
                    self.pinyin_buf = rem
                    self.last_committed_token = text
                    self.pre_context = (self.pre_context + text)[-12:]
                    self.token_history.append(text)
                    self.typer.commit_text(text)
                    self._update_pinyin_candidates()
                else:
                    self._clear_pinyin_buf()
                    self._commit_and_predict_next(text)
                return

            self._rec_seq += 1
            self.canvas.clear()
            self._commit_and_predict_next(text)

    def _on_undo_clicked(self, button):
        self._cancel_auto_commit()
        self.canvas.undo_stroke()

    def _on_clear_clicked(self, button):
        self._cancel_auto_commit()
        self._rec_seq += 1
        self.canvas.clear()
        self._clear_pinyin_buf()
        self._update_candidates(self._default_punctuations(), mode="punctuation")

    def _commit_or_key(self, keycode: int):
        self._cancel_auto_commit()
        # 1. If Pinyin composition buffer is active on Virtual Keyboard
        if self.pinyin_buf:
            if keycode == ecodes.KEY_SPACE and self.candidates:
                self._on_candidate_clicked(self.cand_buttons[0], 0)
                return
            elif keycode == ecodes.KEY_ENTER:
                raw_en = self.pinyin_buf
                self._clear_pinyin_buf()
                self._commit_and_predict_next(raw_en)
                return
            elif keycode == ecodes.KEY_BACKSPACE:
                self.pinyin_buf = self.pinyin_buf[:-1]
                self._update_pinyin_candidates()
                return

        # 2. If Handwriting Canvas has active strokes
        if self.canvas.has_strokes() and self.candidates:
            if keycode in (ecodes.KEY_SPACE, ecodes.KEY_ENTER):
                self._rec_seq += 1
                committed = self.candidates[0]
                self.canvas.clear()
                self._commit_and_predict_next(committed)
                return
            elif keycode == ecodes.KEY_BACKSPACE:
                self._on_clear_clicked(None)
                return

        # 3. Direct keycode pass-through to target window
        if keycode == ecodes.KEY_BACKSPACE:
            self.last_committed_token = ""
            if self.token_history:
                self.token_history.pop()
            self._update_candidates(self._default_punctuations(), mode="punctuation")
        elif keycode in (ecodes.KEY_SPACE, ecodes.KEY_ENTER):
            self.last_committed_token = ""
        self.typer.tap_key(keycode)

    def _resize_for_page(self, page_name: str, recenter: bool = True):
        """Dynamically resize the floating window between compact/wide handwriting ('hw') and full keyboard ('kbd')."""
        sw, sh = self.get_screen_size()
        old_center_x = self.win_x + self.win_w * 0.5
        if page_name == "kbd":
            target_w, target_h = 780, 285
        elif page_name == "sym":
            target_w, target_h = 620, 280
        elif self.wide_hw_mode:
            target_w, target_h = 640, 275
        else:
            target_w, target_h = 420, 270

        if recenter:
            nx = int(old_center_x - target_w * 0.5)
        else:
            nx = self.win_x
        nx = max(12, min(sw - target_w - 12, nx))
        ny = max(24, min(sh - target_h - 24, self.win_y))

        self.win_w, self.win_h = target_w, target_h
        self.win_x, self.win_y = nx, ny
        self.set_size_request(target_w, target_h)
        self.resize(target_w, target_h)
        self.move(nx, ny)

    def _on_switch_to_sym(self, button):
        """Toggle in-place between Handwriting Canvas ('hw') and Full Symbol Panel ('sym')."""
        self._cancel_auto_commit()
        self._stop_bksp_repeat()
        self._clear_pinyin_buf()
        force_im_state_active()
        if self.main_stack.get_visible_child_name() != "sym":
            self.main_stack.set_visible_child_name("sym")
            self._resize_for_page("sym")
            self.sym_btn.set_label("✒ 手写")
            self.osk_btn.set_label("⌨ 键盘")
        else:
            self.main_stack.set_visible_child_name("hw")
            self._resize_for_page("hw")
            self.sym_btn.set_label("※ 符号")
            self.osk_btn.set_label("⌨ 键盘")
        self._update_candidates(self._default_punctuations(reset_page=False), mode="punctuation")

    def _on_switch_to_osk(self, button):
        """Toggle in-place between Handwriting Canvas ('hw') and Wide Full Virtual Keyboard ('kbd')."""
        self._cancel_auto_commit()
        self._stop_bksp_repeat()
        self._clear_pinyin_buf()
        force_im_state_active()
        if self.main_stack.get_visible_child_name() != "kbd":
            self.flush_pending_character()
            self.main_stack.set_visible_child_name("kbd")
            self._resize_for_page("kbd")
            self.osk_btn.set_label("✒ 手写")
            self.sym_btn.set_label("※ 符号")
        else:
            self.main_stack.set_visible_child_name("hw")
            self._resize_for_page("hw")
            self.osk_btn.set_label("⌨ 键盘")
            self.sym_btn.set_label("※ 符号")
        self._update_candidates(self._default_punctuations(reset_page=False), mode="punctuation")

    def show_at_input_box(self, tap_x: float, tap_y: float):
        self.osk_mode = False
        set_pad_visible_flag(True)
        force_im_state_active()
        if not self.get_visible():
            self._clear_pinyin_buf()
            self.main_stack.set_visible_child_name("hw")
            self.osk_btn.set_label("⌨ 键盘")
            self.sym_btn.set_label("※ 符号")
            target_w = 640 if self.wide_hw_mode else 420
            target_h = 275 if self.wide_hw_mode else 270
            self.win_w, self.win_h = target_w, target_h
            self.set_size_request(target_w, target_h)
            self.resize(target_w, target_h)
            self.position_avoiding_point(tap_x, tap_y)
            self.show_all()
            self.move(self.win_x, self.win_y)
        else:
            self.position_avoiding_point(tap_x, tap_y)

    def hide_all_panels(self, preserve_active: bool = False):
        self._cancel_auto_commit()
        self._stop_bksp_repeat()
        self._clear_pinyin_buf()
        self._rec_seq += 1
        self.osk_mode = False
        was_visible = self.get_visible()
        if was_visible:
            self.canvas.clear()
            self._update_candidates(self._default_punctuations(), mode="punctuation")
            self.hide()
        if was_visible or preserve_active:
            force_im_state_active()
            GLib.timeout_add(80, lambda: (force_im_state_active(), set_pad_visible_flag(False)) and False)
        else:
            set_pad_visible_flag(False)


class InputDeviceMonitor(threading.Thread):
    """
    Monitors:
    1. Surface Pen (IPTSD Virtual Stylus): 4096-level pressure, tail eraser (BTN_TOOL_RUBBER), barrel button (BTN_STYLUS)
    2. Touchscreen (IPTSD Virtual Touchscreen): finger interaction on pad & auto-wakeup in Tablet Mode
    3. Type Cover Keyboard / Touchpad & Tablet Mode Switch (SW_TABLET_MODE)
    """

    def __init__(self, window: SurfaceHandwritingWindow):
        super().__init__(daemon=True)
        self.window = window
        self.running = True
        self.grabbed_devs = set()
        self.tablet_mode = False

    def _discover_devices(self):
        stylus_devs = []
        touch_devs = []
        keyboard_devs = []
        sw_devs = []
        pen_bt_devs = []
        has_physical_kbd = False
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            name = (dev.name or "").lower()
            caps = dev.capabilities()
            if "stylus" in name:
                stylus_devs.append(dev)
            elif "iptsd virtual touchscreen" in name:
                touch_devs.append(dev)
            elif "surface pen" in name and "stylus" not in name:
                pen_bt_devs.append(dev)
            elif "surface type cover keyboard" in name or "surface type cover touchpad" in name:
                keyboard_devs.append(dev)
                if "keyboard" in name:
                    has_physical_kbd = True
            elif ecodes.EV_SW in caps and ecodes.SW_TABLET_MODE in caps.get(ecodes.EV_SW, []):
                sw_devs.append(dev)
            else:
                dev.close()
        self.tablet_mode = not has_physical_kbd
        return stylus_devs, touch_devs, keyboard_devs, sw_devs, pen_bt_devs

    def _set_grab(self, dev, should_grab: bool):
        # Never grab the stylus itself mid-proximity.
        return

    def _set_touch_palm_grab(self, touch_devs, should_grab: bool):
        for d in touch_devs:
            is_grabbed = d.fd in self.grabbed_devs
            if should_grab and not is_grabbed:
                try:
                    d.grab()
                    self.grabbed_devs.add(d.fd)
                except OSError:
                    pass
            elif not should_grab and is_grabbed:
                try:
                    d.ungrab()
                except OSError:
                    pass
                self.grabbed_devs.discard(d.fd)

    def _sync_tablet_rotation_lock(self, is_tablet: bool):
        def _run():
            try:
                # Unlock auto-rotation in tablet mode; keep unlocked or user preference
                if is_tablet:
                    subprocess.run(
                        [
                            "gsettings",
                            "set",
                            "org.gnome.settings-daemon.peripherals.touchscreen",
                            "orientation-lock",
                            "false",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.5,
                    )
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def run(self):
        fd_map = {}
        last_kbd_hide = 0.0

        abs_x = 4800
        abs_y = 3600
        abs_pressure = 2048
        abs_tilt_x = 0
        abs_tilt_y = 0
        tool_pen = False
        tool_rubber = False
        btn_stylus = False
        prev_btn_stylus = False
        frame_touch = False
        prev_touch = False
        last_stylus_active = 0.0
        down_sx = 0.0
        down_sy = 0.0
        down_time = 0.0
        down_inside_pad = False

        ts_x = 4800
        ts_y = 3600
        ts_touch = False
        ts_prev_touch = False
        ts_down_sx = 0.0
        ts_down_sy = 0.0
        ts_down_time = 0.0
        ts_down_inside = False
        touch_devs_list = []

        while self.running:
            if not fd_map:
                self.grabbed_devs.clear()
                stylus_devs, touch_devs, keyboard_devs, sw_devs, pen_bt_devs = self._discover_devices()
                touch_devs_list = list(touch_devs)
                for d in stylus_devs:
                    fd_map[d.fd] = ("stylus", d)
                for d in touch_devs:
                    fd_map[d.fd] = ("touch", d)
                for d in keyboard_devs:
                    fd_map[d.fd] = ("kbd", d)
                for d in sw_devs:
                    fd_map[d.fd] = ("sw", d)
                for d in pen_bt_devs:
                    fd_map[d.fd] = ("pen_bt", d)

            if not fd_map:
                time.sleep(1.0)
                continue

            try:
                r, _, _ = select.select(list(fd_map.keys()), [], [], 1.0)
            except (ValueError, OSError):
                for _, d in fd_map.values():
                    self._set_grab(d, False)
                    try:
                        d.close()
                    except OSError:
                        pass
                fd_map.clear()
                continue

            sw, sh = self.window.get_screen_size()
            now_mono = time.monotonic()
            if not (tool_pen or tool_rubber or frame_touch) and (now_mono - last_stylus_active > 0.35):
                self._set_touch_palm_grab(touch_devs_list, False)

            for fd in r:
                role, dev = fd_map.get(fd, (None, None))
                if not dev:
                    continue
                try:
                    for ev in dev.read():
                        if role == "stylus":
                            if ev.type == ecodes.EV_ABS:
                                if ev.code == ecodes.ABS_X:
                                    abs_x = ev.value
                                elif ev.code == ecodes.ABS_Y:
                                    abs_y = ev.value
                                elif ev.code == ecodes.ABS_PRESSURE:
                                    abs_pressure = ev.value
                                elif ev.code == ecodes.ABS_TILT_X:
                                    abs_tilt_x = ev.value
                                elif ev.code == ecodes.ABS_TILT_Y:
                                    abs_tilt_y = ev.value
                            elif ev.type == ecodes.EV_KEY:
                                if ev.code == ecodes.BTN_TOUCH:
                                    frame_touch = (ev.value == 1)
                                elif ev.code == ecodes.BTN_TOOL_PEN:
                                    tool_pen = (ev.value == 1)
                                elif ev.code == ecodes.BTN_TOOL_RUBBER:
                                    tool_rubber = (ev.value == 1)
                                elif ev.code == ecodes.BTN_STYLUS:
                                    btn_stylus = (ev.value == 1)
                            elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
                                last_stylus_active = time.monotonic()
                                if (tool_pen or tool_rubber or frame_touch) and not ts_prev_touch:
                                    self._set_touch_palm_grab(touch_devs_list, True)
                                elif not (tool_pen or tool_rubber or frame_touch):
                                    self._set_touch_palm_grab(touch_devs_list, False)

                                # TipDistance (0.25cm) tilt parallax compensation
                                tilt_dx = math.sin(math.radians(abs_tilt_x / 100.0)) * 3.0 if abs_tilt_x else 0.0
                                tilt_dy = math.sin(math.radians(abs_tilt_y / 100.0)) * 3.0 if abs_tilt_y else 0.0
                                sx = (abs_x / 9600.0) * sw + tilt_dx
                                sy = (abs_y / 7200.0) * sh + tilt_dy
                                norm_p = max(0.08, min(1.0, abs_pressure / 4096.0)) if abs_pressure > 0 else 0.5
                                is_eraser = bool(tool_rubber or btn_stylus)

                                # Handle Barrel Side Button (BTN_STYLUS) press even while hovering!
                                if btn_stylus and not prev_btn_stylus and not frame_touch:
                                    prev_btn_stylus = True
                                    if self.window.get_visible() and self.window.contains_point(sx, sy, margin=28.0):
                                        GLib.idle_add(self.window.on_stylus_down, sx, sy, norm_p, True)
                                        GLib.timeout_add(140, lambda: self.window.on_stylus_up(sx, sy) or False)
                                    else:
                                        GLib.idle_add(self.window.show_at_input_box, sx, sy)
                                elif not btn_stylus:
                                    prev_btn_stylus = False

                                if frame_touch and not prev_touch:
                                    prev_touch = True
                                    down_sx, down_sy = sx, sy
                                    down_time = time.time()
                                    if self.window.get_visible() and self.window.contains_point(sx, sy, margin=12.0):
                                        down_inside_pad = True
                                        self._set_grab(dev, True)
                                        GLib.idle_add(self.window.on_stylus_down, sx, sy, norm_p, is_eraser)
                                    elif is_eraser and not self.window.get_visible():
                                        down_inside_pad = False
                                        GLib.idle_add(self.window.show_at_input_box, sx, sy)
                                    else:
                                        down_inside_pad = False
                                        self._set_grab(dev, False)
                                        if self.window.get_visible():
                                            GLib.idle_add(self.window.flush_pending_character)

                                elif frame_touch and prev_touch:
                                    if down_inside_pad and self.window.get_visible():
                                        GLib.idle_add(self.window.on_stylus_motion, sx, sy, norm_p)

                                elif not frame_touch and prev_touch:
                                    prev_touch = False
                                    if down_inside_pad:
                                        down_inside_pad = False
                                        GLib.idle_add(self.window.on_stylus_up, sx, sy)
                                    else:
                                        dt = time.time() - down_time
                                        dist = math.hypot(sx - down_sx, sy - down_sy)
                                        tap_x, tap_y = sx, sy
                                        if dt < 0.65 and dist < 45.0:
                                            def check_input_box_tap(tx=tap_x, ty=tap_y):
                                                is_active, _, _ = read_im_state()
                                                if is_active:
                                                    self.window.show_at_input_box(tx, ty)
                                                else:
                                                    self.window.hide_all_panels()
                                                return False

                                            GLib.timeout_add(90, check_input_box_tap)

                                else:
                                    if self.window.get_visible():
                                        inside = self.window.contains_point(sx, sy, margin=18.0)
                                        self._set_grab(dev, inside)
                                        if inside:
                                            GLib.idle_add(self.window.on_stylus_hover, sx, sy)
                                        else:
                                            GLib.idle_add(self.window.on_stylus_hover, -100.0, -100.0)
                                    else:
                                        self._set_grab(dev, False)

                        elif role == "touch":
                            if ev.type == ecodes.EV_ABS:
                                if ev.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X):
                                    ts_x = ev.value
                                elif ev.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y):
                                    ts_y = ev.value
                            elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                                ts_touch = (ev.value == 1)
                            elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
                                # Suppress touch/palm events when Surface Pen is in proximity or writing
                                if tool_pen or tool_rubber or frame_touch or (time.monotonic() - last_stylus_active < 0.45):
                                    ts_prev_touch = False
                                    ts_down_inside = False
                                    continue
                                tsx = (ts_x / 9600.0) * sw
                                tsy = (ts_y / 7200.0) * sh
                                if ts_touch and not ts_prev_touch:
                                    ts_prev_touch = True
                                    ts_down_sx, ts_down_sy = tsx, tsy
                                    ts_down_time = time.time()
                                    if self.window.get_visible() and self.window.contains_point(tsx, tsy, margin=8.0):
                                        ts_down_inside = True
                                        GLib.idle_add(self.window.on_stylus_down, tsx, tsy, 0.55, False)
                                    else:
                                        ts_down_inside = False
                                elif ts_touch and ts_prev_touch:
                                    if ts_down_inside and self.window.get_visible():
                                        GLib.idle_add(self.window.on_stylus_motion, tsx, tsy, 0.55)
                                elif not ts_touch and ts_prev_touch:
                                    ts_prev_touch = False
                                    if ts_down_inside:
                                        ts_down_inside = False
                                        GLib.idle_add(self.window.on_stylus_up, tsx, tsy)
                                    elif self.tablet_mode:
                                        dt = time.time() - ts_down_time
                                        dist = math.hypot(tsx - ts_down_sx, tsy - ts_down_sy)
                                        tap_x, tap_y = tsx, tsy
                                        if dt < 0.38 and dist < 28.0:
                                            def check_tablet_tap(tx=tap_x, ty=tap_y):
                                                is_active, _, _ = read_im_state()
                                                if is_active:
                                                    self.window.show_at_input_box(tx, ty)
                                                else:
                                                    self.window.hide_all_panels()
                                                return False

                                            GLib.timeout_add(95, check_tablet_tap)

                        elif role == "sw":
                            if ev.type == ecodes.EV_SW and ev.code == ecodes.SW_TABLET_MODE:
                                self.tablet_mode = (ev.value == 1)
                                self._sync_tablet_rotation_lock(self.tablet_mode)

                        elif role == "pen_bt":
                            if ev.type == ecodes.EV_KEY and ev.value == 1:
                                self.window.refresh_pen_battery_async()
                                if ev.code == ecodes.KEY_F20:
                                    def toggle_pad():
                                        if self.window.get_visible():
                                            self.window.hide_all_panels()
                                        else:
                                            sw_w, sw_h = self.window.get_screen_size()
                                            self.window.show_at_input_box(sw_w * 0.5, sw_h * 0.82)
                                        return False
                                    GLib.idle_add(toggle_pad)

                        elif role == "kbd":
                            if ev.type == ecodes.EV_KEY and ev.value == 1:
                                self.tablet_mode = False
                                t = time.monotonic()
                                if t - last_kbd_hide > 0.2:
                                    last_kbd_hide = t
                                    for _, d in fd_map.values():
                                        self._set_grab(d, False)
                                    GLib.idle_add(self.window.hide_all_panels, True)
                except OSError as e:
                    if e.errno == errno.ENODEV:
                        for _, d in fd_map.values():
                            self._set_grab(d, False)
                            try:
                                d.close()
                            except OSError:
                                pass
                        fd_map.clear()
                        break


def main():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            if "--show" in sys.argv or "--toggle" in sys.argv:
                os.kill(old_pid, signal.SIGUSR1)
                return
        except (ValueError, OSError):
            pass

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    win = SurfaceHandwritingWindow()

    def on_sigusr1():
        if win.get_visible():
            win.hide_all_panels()
        else:
            win.osk_mode = False
            sw, sh = win.get_screen_size()
            win.show_at_input_box(sw * 0.5, sh * 0.85)
        return True

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, on_sigusr1)

    if "--show" in sys.argv:
        sw, sh = win.get_screen_size()
        GLib.idle_add(win.show_at_input_box, sw * 0.5, sh * 0.85)
    monitor = InputDeviceMonitor(win)
    monitor.start()
    Gtk.main()


if __name__ == "__main__":
    main()
