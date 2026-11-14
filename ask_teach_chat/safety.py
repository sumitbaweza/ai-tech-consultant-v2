"""Safe answers (Task 3.6): the injection shield and the hand-off rules.

Three layers against prompt injection, cheapest first:
1. Rules spot the usual tricks in the pasted message (ignore your rules, show your prompt,
   act as an admin, list every customer, fake <system> tags, encoded blobs).
2. Fencing: whatever the customer wrote goes inside <customer_message> tags, and the master
   prompt says text in those tags (and in crawled pages and emails) is data, never instructions.
3. An output check: every system prompt carries a random marker (the canary). If the reply
   contains it, or repeats the system prompt word for word, the reply is withheld.

Angry, refund and legal messages always go to a human (Task 3.6). Rules decide clear cases for
free; a cheap model (the "chat.risk_check" prompt) decides the unclear ones, and when no model
is available an unclear case goes to a human too: it costs a person a minute, not a customer.
"""

import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

Risk = Literal["none", "angry", "refund", "legal"]

INJECTION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ignore|disregard|forget|skip|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|the|any)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?|directions?)\b",
        r"\b(reveal|show|print|repeat|output|leak|display|tell me|give me|what (is|are|were))\b.{0,30}\b(your|the)\b.{0,25}\b(system|hidden|secret|initial|original|internal)?\s*(prompt|instructions?|rules|message|configuration)\b",
        r"\b(you are now|from now on you are|act as|pretend (to be|you are)|roleplay as|switch to)\b.{0,40}\b(admin|administrator|developer|root|dan|system|unrestricted|jailbroken|different (ai|assistant))\b",
        r"\b(developer|god|debug|admin|maintenance) mode\b|\bjailbreak\b|\bdo anything now\b",
        r"\b(list|show|give|export|dump|send|reveal|share)\b.{0,30}\b(all|every|other|each)\b.{0,20}\b(customers?|users?|members?|accounts?|clients?|emails?|orders?|records|data)\b",
        r"\b(api[ _-]?keys?|passwords?|secrets?|tokens?|credentials?)\b.{0,30}\b(of|for|from)\b.{0,15}\b(all|other|every|the) ",
        r"</?\s*(system|instructions?|assistant|developer|tool|function|admin)\s*>",
        r"\b(begin|start|end)\s+(system|admin|developer)\s+(prompt|message|instructions?)\b|#{2,}\s*(system|instructions?)\b",
        r"\b(override|disable|turn off|remove)\b.{0,20}\b(your|the|all|any)\b.{0,20}\b(safety|rules|filters?|restrictions?|guardrails?|policy|policies)\b",
        r"\b(run|execute|eval)\b.{0,20}\b(this|the following|my)\b.{0,10}\b(code|command|sql|script|query)\b",
        r"\b(translate|summari[sz]e|repeat)\b.{0,20}\b(everything|all|the text)\b.{0,20}\b(above|before this|so far)\b",
        r"\b(new|updated|real)\s+(instructions?|rules)\s*:",
    )
]
ENCODED_BLOB = re.compile(r"[A-Za-z0-9+/=]{180,}")

REFUND = re.compile(
    r"\b(refunds?|refunded|money back|chargebacks?|charge ?backs?|reimburse(ment)?|dispute (the|a|this|my) (charge|payment)|"
    r"charged (me )?(twice|double|again)|double[- ]charged|overcharged|cancel\w*\b.{0,40}\b(refund|money|charge)|"
    r"want my money|return my money|pay me back)\b",
    re.IGNORECASE,
)
LEGAL = re.compile(
    r"\b(lawyers?|attorneys?|solicitors?|legal action|take (you|this) to court|court|sue|suing|lawsuit|small claims|"
    r"ombudsman|regulator|gdpr|data protection (authority|officer)|cease and desist|breach of contract|"
    r"consumer protection|trading standards|legal department|my rights under)\b",
    re.IGNORECASE,
)
ANGRY = re.compile(
    r"\b(furious|outraged|unacceptable|ridiculous|disgusting|disgraceful|terrible service|horrible|worst|scam|scammers|"
    r"fraud|rip-?off|pathetic|useless|incompetent|fed up|sick (and tired )?of|livid|angry|so frustrated|"
    r"waste of (money|time)|never again|shame on you|joke of a)\b",
    re.IGNORECASE,
)
# Words that may or may not mean trouble: the cheap model decides
UNSURE = re.compile(
    r"\b(charge[ds]?|billing|bill|invoice|payment|cancel\w*|disappointed|frustrat\w*|upset|complain\w*|"
    r"not happy|unhappy|delete my (account|data)|my data)\b",
    re.IGNORECASE,
)


@dataclass
class SafetyScan:
    injection: bool = False
    injection_hits: list[str] = field(default_factory=list[str])
    risk: Risk = "none"
    risk_hits: list[str] = field(default_factory=list[str])
    unsure: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "injection": self.injection,
            "injection_hits": self.injection_hits[:5],
            "risk": self.risk,
            "risk_hits": self.risk_hits[:5],
            "unsure": self.unsure,
        }


def _shouting(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 40 and sum(c.isupper() for c in letters) / len(letters) > 0.6


def scan(text: str) -> SafetyScan:
    """Rules only: free, instant, and the same answer every time."""
    result = SafetyScan()
    for pattern in INJECTION:
        if match := pattern.search(text):
            result.injection_hits.append(match.group(0)[:80])
    if ENCODED_BLOB.search(text):
        result.injection_hits.append("long encoded text")
    result.injection = bool(result.injection_hits)
    for risk, pattern in (("legal", LEGAL), ("refund", REFUND), ("angry", ANGRY)):
        if match := pattern.search(text):
            result.risk_hits.append(f"{risk}: {match.group(0)}")
            if result.risk == "none":
                result.risk = risk  # type: ignore[assignment]
    exclaiming = re.search(r"!{3,}", text) and re.search(
        r"\b(not|never|still|again|nobody|no one)\b", text, re.IGNORECASE
    )
    if result.risk == "none" and (_shouting(text) or exclaiming):
        result.risk, result.risk_hits = "angry", ["shouting"]
    result.unsure = result.risk == "none" and bool(UNSURE.search(text))
    return result


def new_canary() -> str:
    """A random marker for this one answer's system prompt (layer 3)."""
    return f"CANARY-{secrets.token_hex(6)}"


def leaked(reply: str, canary: str, system_text: str) -> str | None:
    """Why the reply must be withheld, or None. Checks the canary, and copies of the system prompt
    (any 10 words in a row, the canary itself left out of the comparison)."""
    if canary in reply:
        return "the reply contained the system prompt's private marker"
    words = re.findall(r"\w+", system_text.replace(canary, " ").lower())
    reply_words = " " + " ".join(re.findall(r"\w+", reply.lower())) + " "
    for start in range(0, max(len(words) - 10, 0) + 1, 3):
        window = " ".join(words[start : start + 10])
        if len(window) > 40 and f" {window} " in reply_words:
            return "the reply repeated the system prompt"
    return None


HANDOFF_TEXT = {
    "angry": "This customer is upset. A person should reply.",
    "refund": "This is about a refund or a charge. A person from billing should reply.",
    "legal": "This mentions legal action or legal rights. A person must reply.",
    "role": "The role's hand-over rule applies. A person should reply.",
}

HOLDING_REPLY = (
    "Thank you for your message. I've passed it to a member of our team, who will reply to you "
    "personally as soon as possible."
)
REFUSAL = (
    "I can only help with questions about our product and your account. What can I help you with?"
)
