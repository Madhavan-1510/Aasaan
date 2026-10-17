"""Decision gates (Jev, then Jev via OpenRouter, then Mercury Decide, then Gemma, then a rule), all through `router.decide`:

  page_health    after each page fetch: real content, or a cookie wall / login / CAPTCHA / error page? (-> try the next fetch tier)
  claim_support  before the Critic: which claims in the notes are not supported by the sources? (only those go to the Critic)
  scoper         what track is this and what level? (student data: never sent to the free-tier Mercury)
  video_marker   which chapter marker of a video matches the chapter?
  quiz_sanity    does each question have exactly one correct, unambiguous answer?
  input_gate     is the chat-box request something we can build a course from? (student data)

Every gate has a code fallback for when no decision provider answers, and never blocks work on its own failure.
Low confidence is handled by the router (it escalates only the unsure questions); every escalation is an event.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Optional

from ..providers.typesafe import choice, noul, score
from ..router import ModelRouter
from ..schemas import QuizOut, resolve_key

INPUT_GATE_BUDGET_S = 1.5
LEVELS = ["beginner", "intermediate", "advanced"]
TRACKS = {"programming": "learning a programming language or software development", "dsa": "data structures and algorithms",
          "web": "web, apps, cloud or devops", "data_ml": "data science, statistics, machine learning or AI",
          "math": "mathematics", "science": "physics, chemistry, biology or engineering", "language": "a human language or writing",
          "exam": "preparing for a specific exam", "humanities": "history, economics, law, arts or social science", "other": "something else"}


def _p(ans: Optional[dict]) -> Optional[float]:
    try:
        return float(ans["noul"])
    except (KeyError, TypeError, ValueError):
        return None


# ===================================================================== page_health
BAD_PAGE = re.compile(r"(verify (that )?you are (a )?human|captcha|enable javascript|javascript is (disabled|required)|are you a robot|access denied|"
                      r"403 forbidden|404 not found|page not found|accept (all )?cookies|we use cookies|sign in to continue|log in to continue|"
                      r"subscribe to (read|continue)|attention required|just a moment)", re.I)


def page_rule(text: str) -> Optional[str]:
    """Code check: a short page that is mostly a wall message."""
    m = BAD_PAGE.search(text or "")
    return f"looks like a wall or error page ('{m.group(0)[:30]}')" if m and len(text) < 1500 else None


def page_health(router: ModelRouter, url: str, title: str, text: str) -> tuple[bool, str]:
    """(healthy, reason). A page that is not the real content makes the fetcher try its next tier."""
    rule = page_rule(text)
    if rule:
        return False, rule
    q = {"real": noul("Is this the real content of the page a reader wanted, rather than a cookie/consent wall, login or paywall stub, CAPTCHA, error page or empty shell?",
                      "Real, readable content about a subject", "A wall, login, CAPTCHA, error page or an empty shell")}
    ans = router.decide("page_health", {"url": url, "title": title, "start_of_page": (text or "")[:1500]}, q, summary=f"page health {url[:60]}", timeout_s=8)
    p = _p((ans or {}).get("real"))
    if p is None:
        return True, ""            # no provider answered: the length check already passed, accept
    return (p >= 0.5), ("" if p >= 0.5 else f"page_health judged it not real content ({p:.2f})")


# =================================================================== claim_support
def claim_sentences(notes_md: str, limit: int = 12) -> list[str]:
    body = re.sub(r"```.*?```", " ", notes_md or "", flags=re.S)
    out = []
    for line in body.splitlines():
        line = re.sub(r"^[\s>*\-#\d.]+", "", line).strip()
        for s in re.split(r"(?<=[.!?])\s+", line):
            s = re.sub(r"[*_`]", "", s).strip()
            if len(s.split()) >= 6 and not s.endswith("?"):
                out.append(s)
    return out[:limit]


def claim_support(router: ModelRouter, notes_md: str, chunks: list[dict]) -> Optional[list[str]]:
    """Claims the sources do not support (empty list = all supported), or None when the gate could not run (no sources, no provider)."""
    claims = claim_sentences(notes_md)
    if not chunks or not claims:
        return None
    state = {"sources": [{"id": c["chunk_id"], "text": c["text"][:500]} for c in chunks[:8]]}
    qs = {f"c{i}": noul(f"Is this claim supported by the sources, with nothing contradicted or invented? Claim: {c}",
                        "The sources state or clearly imply it", "Not in the sources, or contradicted") for i, c in enumerate(claims)}
    ans = router.decide("claim_support", state, qs, summary=f"claim support for {len(claims)} claim(s)")
    if not ans:
        return None
    flagged = []
    for i, c in enumerate(claims):
        a = ans.get(f"c{i}")
        p = _p(a)
        if p is None or p < 0.6 or (a or {}).get("unsure"):
            flagged.append(c)       # unsure counts as flagged: the Critic looks at it
    return flagged


# ======================================================================== scoper
def scoper(router: ModelRouter, goal: str, topics: list[dict]) -> Optional[dict]:
    """{"track", "level", "by"} or None. Student data: the free-tier decision model is never used."""
    if not goal and not topics:
        return None
    state = {"goal": goal, "topics": [t.get("title", "") for t in topics[:10]]}
    qs = {"track": choice("Which kind of learning is this?", TRACKS),
          "level": score("How advanced is the level the student is asking for or showing? Pick the closest.", LEVELS)}
    ans = router.decide("scoper", state, qs, student_data=True, summary="scope the request")
    if not ans:
        return None
    out: dict = {"by": (ans.get("track") or ans.get("level") or {}).get("_by", "?")}
    t = ans.get("track")
    if t and t.get("choice") in TRACKS:
        out["track"] = t["choice"]
    lv = ans.get("level")
    if lv:
        try:
            if not lv.get("unsure"):
                out["level"] = LEVELS[max(0, min(2, round(float(lv["score"]))))]
        except (KeyError, TypeError, ValueError):
            pass
    return out if len(out) > 1 else None


# ==================================================================== video_marker
def video_marker(router: ModelRouter, chapter_title: str, videos: dict[str, list[dict]]) -> dict[str, int]:
    """{video id: start seconds} for videos with chapter markers where one marker clearly matches the chapter. One batched call."""
    qs, tables = {}, {}
    for vid, marks in videos.items():
        if len(marks) < 2:
            continue
        opts = {f"m{i}": m["title"] for i, m in enumerate(marks[:20])}
        opts["none"] = "no marker matches this chapter; start from the beginning"
        qs[vid] = choice(f"Which marker of this video starts the part that teaches '{chapter_title}'?", opts)
        tables[vid] = marks
    if not qs:
        return {}
    ans = router.decide("video_marker", {"chapter": chapter_title}, qs, summary=f"video markers for '{chapter_title[:40]}'") or {}
    out = {}
    for vid, a in ans.items():
        k = a.get("choice", "")
        if a.get("unsure") or not k.startswith("m"):
            continue
        try:
            out[vid] = int(tables[vid][int(k[1:])]["start"])
        except (ValueError, IndexError, KeyError):
            continue
    return out


def parse_markers(description: str) -> list[dict]:
    """Chapter markers from a YouTube description: lines like `12:30 Binary search` (first one must be 0:00, as YouTube requires)."""
    marks = []
    for m in re.finditer(r"(?m)^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*[-–—:]?\s*(.{2,80})$", description or ""):
        h, mi, sec, title = int(m.group(1) or 0), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        marks.append({"start": h * 3600 + mi * 60 + sec, "title": title})
    return marks if marks and marks[0]["start"] == 0 and len(marks) >= 3 else []


# ===================================================================== quiz_sanity
def quiz_sanity(router: ModelRouter, quiz: QuizOut, notes_md: str) -> tuple[QuizOut, list[str]]:
    """Drop questions whose answer is ambiguous or wrong, as long as 3 remain. Returns (quiz, human-readable notes)."""
    state = {"notes": (notes_md or "")[:2500]}
    qs = {}
    for q in quiz.questions:
        key = resolve_key(q)
        qs[q.id] = noul(f"Question: {q.text}\nOptions: {' | '.join(q.options)}\nMarked correct: {key}\nIs exactly ONE option correct, and is it the marked one?",
                        "Exactly one option is correct and it is the marked one", "Several options could be right, none is, or the marked one is wrong")
    ans = router.decide("quiz_sanity", state, qs, summary=f"quiz sanity for {len(qs)} question(s)")
    if not ans:
        return quiz, []
    bad = {qid for qid, a in ans.items() if (_p(a) is not None and _p(a) < 0.4)}
    if not bad:
        return quiz, []
    keep = [q for q in quiz.questions if q.id not in bad]
    if len(keep) < 3:
        return quiz, [f"quiz_sanity doubted {len(bad)} question(s) but fewer than 3 would remain, so the quiz was kept"]
    return QuizOut(questions=keep), [f"quiz_sanity dropped {len(bad)} ambiguous question(s): {', '.join(sorted(bad))}"]


# ===================================================================== chat intent
INTENTS = {"ask_doubt": "a question about the course content or the student's own material",
           "explain": "asks to explain a chapter or part again, simpler, at a different level, or with another example",
           "edit_course": "asks to change the course: add, remove, move or rewrite chapters, change the level, focus or track",
           "add_resource": "wants to add or use a file, link, video or notes they are providing",
           "quiz_me": "wants to be quizzed or to practise on a chapter",
           "other": "chit-chat, off-topic, or anything not about their course"}
INTENT_BUDGET_S = 4.0
_RULES = [("quiz_me", re.compile(r"\b(quiz me|test me|give me (a )?quiz|practice questions?|quiz on)\b", re.I)),
          ("edit_course", re.compile(r"\b((add|insert|create|include)\b.*\bchapters?|(remove|delete|drop|skip)\b.*\bchapters?|move\b.*\bchapters?|reorder|rearrange|regenerate|rewrite|redo "
                                     r"chapter|change (the )?level|make (it|the course) (more |a bit )?(beginner|easier|harder|advanced|intermediate)|focus (more )?on)\b", re.I)),
          ("add_resource", re.compile(r"\b(add|use|read|include)\b.*\b(this|these|my)\b.*\b(pdf|file|link|video|article|notes|document)\b", re.I)),
          ("explain", re.compile(r"\b(explain|re-?explain|simpler|in simple|differently|eli5|analogy|another example)\b", re.I))]


def route_intent_rules(text: str) -> str:
    for name, rx in _RULES:
        if rx.search(text or ""):
            return name
    return "ask_doubt"


def route_intent(router: ModelRouter, text: str, state: dict) -> dict:
    """{"intent", "by", "unsure"}: one Choice through the decision chain (student data: never Mercury), bounded so a chat turn never stalls on
    it, with a keyword rule as the last fallback. A low-confidence answer asks the student one short clarifying question instead of guessing."""
    ask = lambda: router.decide("intent", state, {"intent": choice("What does the student want with this message?", INTENTS)},  # noqa: E731
                                student_data=True, summary="chat intent", timeout_s=4)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        ans = pool.submit(ask).result(timeout=INTENT_BUDGET_S)
    except FutureTimeout:
        router.note("intent", "fallback", "intent decided by keyword rules", f"no decision within {INTENT_BUDGET_S}s")
        ans = None
    finally:
        pool.shutdown(wait=False)
    a = (ans or {}).get("intent")
    if a and a.get("choice") in INTENTS:
        return {"intent": a["choice"], "by": a.get("_by", "?"), "unsure": bool(a.get("unsure"))}
    return {"intent": route_intent_rules(text), "by": "rules", "unsure": False}


# ====================================================================== input_gate
def input_gate(router: ModelRouter, goal: str) -> Optional[str]:
    """A friendly refusal message, or None to go ahead. Only blocks when the decision is confident; student data (no Mercury)."""
    if not goal or len(goal.split()) < 2:
        return None
    ask = lambda: router.decide("input_gate", {"message": goal[:600]}, {"kind": choice(  # noqa: E731
        "What is this message?", {"learn_request": "the student wants to learn or study something",
                                  "not_a_request": "nonsense, a greeting, or keyboard mashing with no topic",
                                  "unsafe": "asks for help with harming people, hateful, sexual or other clearly unsafe content, or wants to abuse the app"})},
        student_data=True, summary="input gate", timeout_s=6)
    # The student is waiting on the submit button: the gate gets INPUT_GATE_BUDGET_S in total, then the request goes ahead (and says so).
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        ans = pool.submit(ask).result(timeout=INPUT_GATE_BUDGET_S)
    except FutureTimeout:
        router.note("input_gate", "fallback", "input gate skipped: no answer in time", f"no decision within {INPUT_GATE_BUDGET_S}s; the request went ahead")
        return None
    finally:
        pool.shutdown(wait=False)
    a = (ans or {}).get("kind")
    if not a or a.get("unsure"):
        return None
    conf = float(a.get("confidence", 0.7) or 0.7)
    if a.get("choice") == "unsafe" and conf >= 0.7:
        return "I can't build a course from that. Tell me something you'd like to learn, like 'Teach me photosynthesis'."
    if a.get("choice") == "not_a_request" and conf >= 0.85:
        return "I couldn't tell what you want to learn. Try 'Teach me binary search' or paste your notes."
    return None
