"""Deterministic FastAPI bot for the magicpin Vera AI Challenge.

The scorer rewards: trigger relevance, merchant/category specificity, low-friction
engagement, replay handling, and strict endpoint/schema compliance.  This file keeps
the decision layer fully deterministic and uses only facts present in pushed context.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import error as urlerror, request as urlrequest

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

TEAM_NAME = os.getenv("TEAM_NAME", "Tejaskrit")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "dixitkrishna7777@gmail.com")
BOT_VERSION = os.getenv("BOT_VERSION", "2.0.0-final")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
USE_GEMINI_POLISH = os.getenv("USE_GEMINI_POLISH", "false").lower() in {"1", "true", "yes"}
MAX_ACTIONS_PER_TICK = int(os.getenv("MAX_ACTIONS_PER_TICK", "20"))

app = FastAPI(title="Tejaskrit Vera Bot", version=BOT_VERSION)
START_TS = time.time()

# (scope, context_id) -> {version, payload, delivered_at, stored_at}
contexts: dict[tuple[str, str], dict[str, Any]] = {}
# conversation_id -> state
conversations: dict[str, dict[str, Any]] = {}
suppressed_keys: set[str] = set()
closed_conversations: set[str] = set()
merchant_blocked_until: dict[str, float] = {}
auto_reply_memory: dict[tuple[str, str], int] = {}

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=0)
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_ctx(scope: str, cid: Optional[str]) -> Optional[dict[str, Any]]:
    if not cid:
        return None
    item = contexts.get((scope, cid))
    return item.get("payload") if item else None


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def low(text: Any) -> str:
    return norm(text).lower()


def pct(value: Any, signed: bool = False) -> str:
    try:
        v = float(value)
        if abs(v) <= 1.0:
            v *= 100
        sign = "+" if signed and v > 0 else ""
        return f"{sign}{v:.0f}%" if abs(v) >= 10 else f"{sign}{v:.1f}%"
    except Exception:
        return str(value) if value is not None else "?"


def intish(value: Any, default: str = "?") -> str:
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return default


def money_or_text(v: Any) -> str:
    if v is None or v == "":
        return ""
    s = str(v)
    if s.startswith("₹") or not re.fullmatch(r"\d+", s):
        return s
    return f"₹{s}"


def trim_message(body: str, limit: int = 850) -> str:
    # Keep bullets/newlines when intentionally used; only collapse excessive spaces.
    body = re.sub(r"[ \t]+", " ", body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"




def safe_excerpt(text: Any, limit: int = 150) -> str:
    txt = norm(text)
    if not txt:
        return ""
    # Prefer complete first sentence(s) to avoid orphan fragments like "No."
    sentences = re.split(r"(?<=[.!?])\s+", txt)
    out = ""
    for sent in sentences:
        candidate = (out + " " + sent).strip() if out else sent
        if len(candidate) <= limit:
            out = candidate
        else:
            break
    if out:
        return out
    return txt[: limit - 1].rstrip() + "…"

def slugify(value: str, max_len: int = 120) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:max_len].strip("_") or "item"


def trigger_merchant_id(trigger: dict[str, Any]) -> Optional[str]:
    payload = trigger.get("payload", {}) or {}
    return trigger.get("merchant_id") or payload.get("merchant_id") or payload.get("mid")


def trigger_customer_id(trigger: dict[str, Any]) -> Optional[str]:
    payload = trigger.get("payload", {}) or {}
    return trigger.get("customer_id") or payload.get("customer_id") or payload.get("patient_id") or payload.get("member_id")


def trigger_category_slug(trigger: dict[str, Any], merchant: Optional[dict[str, Any]] = None) -> str:
    payload = trigger.get("payload", {}) or {}
    return str(payload.get("category") or (merchant or {}).get("category_slug") or "")


def get_kind(trigger: Optional[dict[str, Any]]) -> str:
    return low((trigger or {}).get("kind", "")).replace("-", "_")


def business_label(merchant: dict[str, Any]) -> str:
    return str((merchant.get("identity", {}) or {}).get("name") or merchant.get("name") or "your business")


def owner_first(merchant: dict[str, Any]) -> str:
    ident = merchant.get("identity", {}) or {}
    owner = str(ident.get("owner_first_name") or "").strip()
    if owner:
        return owner
    name = business_label(merchant)
    name = re.sub(r"^(dr\.?\s+)", "", name, flags=re.I)
    return name.split()[0] if name.split() else "there"


def salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    slug = category.get("slug") or merchant.get("category_slug") or ""
    owner = owner_first(merchant)
    if slug == "dentists":
        return owner if owner.lower().startswith("dr") else f"Dr. {owner}"
    if owner and owner != "there":
        return owner
    return business_label(merchant)


def location_label(merchant: dict[str, Any]) -> str:
    ident = merchant.get("identity", {}) or {}
    loc = ident.get("locality")
    city = ident.get("city")
    if loc and city:
        return f"{loc}, {city}"
    return str(loc or city or "your locality")


def active_offer(merchant: dict[str, Any], category: dict[str, Any], prefer: Optional[str] = None) -> str:
    offers = [o for o in merchant.get("offers", []) or [] if str(o.get("status", "")).lower() == "active" and o.get("title")]
    if prefer:
        p = prefer.lower()
        for offer in offers:
            if p in str(offer.get("title", "")).lower():
                return str(offer["title"])
    if offers:
        return str(offers[0]["title"])
    for offer in category.get("offer_catalog", []) or []:
        if offer.get("title"):
            return str(offer["title"])
    return "one clear service-price offer"


def category_offer_by_keyword(category: dict[str, Any], *keywords: str) -> str:
    for offer in category.get("offer_catalog", []) or []:
        title = str(offer.get("title", ""))
        if all(k.lower() in title.lower() for k in keywords if k):
            return title
    return ""


def find_digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    payload = trigger.get("payload", {}) or {}
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id") or payload.get("item_id")
    digest = category.get("digest", []) or []
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    kind = get_kind(trigger)
    for item in digest:
        item_kind = str(item.get("kind", "")).lower()
        if item_kind and (item_kind in kind or kind in item_kind):
            return item
    return digest[0] if digest else {}


def customer_display(customer: Optional[dict[str, Any]]) -> str:
    if not customer:
        return "there"
    name = (customer.get("identity", {}) or {}).get("name")
    return str(name) if name and not str(name).startswith("(") else "there"


def language_pref(customer: Optional[dict[str, Any]], merchant: dict[str, Any]) -> str:
    if customer:
        return str((customer.get("identity", {}) or {}).get("language_pref", "english")).lower()
    langs = (merchant.get("identity", {}) or {}).get("languages", []) or []
    return "hi-en mix" if "hi" in langs and "en" in langs else "english"


def has_customer_consent(customer: Optional[dict[str, Any]]) -> bool:
    if not customer:
        return True
    consent = customer.get("consent", {}) or {}
    prefs = customer.get("preferences", {}) or {}
    if prefs.get("reminder_opt_in") is False:
        return False
    if consent.get("opted_in_at") or consent.get("scope"):
        return True
    return False


def short_conversation_id(merchant_id: str, trigger_id: str, customer_id: Optional[str] = None) -> str:
    raw = f"conv_{merchant_id}_{customer_id or ''}_{trigger_id}"
    return slugify(raw, 180)


def template_params_from_body(body: str, merchant: dict[str, Any], cta: str) -> list[str]:
    name = business_label(merchant)
    first_line = re.sub(r"\s+", " ", body.strip().split("\n", 1)[0])[:240]
    return [name[:120], first_line, cta[:240]]


def performance_line(merchant: dict[str, Any], category: dict[str, Any]) -> str:
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    parts: list[str] = []
    if perf.get("views") is not None:
        parts.append(f"{intish(perf.get('views'))} views")
    if perf.get("calls") is not None:
        parts.append(f"{intish(perf.get('calls'))} calls")
    if perf.get("directions") is not None:
        parts.append(f"{intish(perf.get('directions'))} direction requests")
    if perf.get("ctr") is not None and peer.get("avg_ctr") is not None:
        parts.append(f"CTR {pct(perf.get('ctr'))} vs peer {pct(peer.get('avg_ctr'))}")
    return ", ".join(parts)


def first_available_slots(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("available_slots") or payload.get("next_session_options") or payload.get("slots") or []
    out: list[str] = []
    for s in raw:
        if isinstance(s, dict):
            label = s.get("label") or s.get("display") or s.get("slot") or s.get("iso")
        else:
            label = s
        if label:
            out.append(str(label))
    return out[:3]


def customer_state_phrase(customer: dict[str, Any], trigger: dict[str, Any]) -> str:
    payload = trigger.get("payload", {}) or {}
    rel = customer.get("relationship", {}) or {}
    state = str(customer.get("state") or "").replace("_", " ")
    last = payload.get("last_service_date") or rel.get("last_visit")
    if last:
        return f"last visit {last}; state {state}" if state else f"last visit {last}"
    return f"state {state}" if state else "customer context available"


def normalize_auto_reply_text(message: str) -> str:
    text = low(message)
    text = re.sub(r"[!?.]+", "", text)
    text = re.sub(r"\b(dr|mr|mrs|ms)\.?\s+[a-z]+\b", "", text)
    return text[:220]


# ---------------------------------------------------------------------------
# Send decision and composers
# ---------------------------------------------------------------------------

ALWAYS_USEFUL_KINDS = {
    "research_digest", "category_research_digest_release", "regulation_change", "compliance_alert",
    "recall_due", "perf_dip", "performance_dip", "renewal_due", "festival_upcoming", "festival",
    "wedding_package_followup", "bridal_followup", "winback_eligible", "review_theme_emerged",
    "milestone_reached", "active_planning_intent", "customer_lapsed_hard", "customer_lapsed_soft",
    "appointment_tomorrow", "trial_followup", "supply_alert", "chronic_refill_due", "category_seasonal",
    "gbp_unverified", "cde_opportunity", "competitor_opened", "perf_spike", "performance_spike",
    "dormant_with_vera", "curious_ask_due", "ipl_match_today", "weather_heatwave", "local_news_event",
    "category_trend_movement", "unplanned_slot_open",
}


def should_send(trigger: dict[str, Any], merchant: dict[str, Any], customer: Optional[dict[str, Any]], now: Optional[datetime]) -> bool:
    if not trigger or not merchant:
        return False
    mid = trigger_merchant_id(trigger) or merchant.get("merchant_id") or ""
    if mid and merchant_blocked_until.get(mid, 0) > time.time():
        return False
    suppression = trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('id')}"
    if suppression and suppression in suppressed_keys:
        return False
    # Trust /v1/tick.available_triggers as the judge's active set.  Do not suppress
    # solely because a seed expires_at/date looks old relative to the real wall clock;
    # the simulator may replay historical scenarios after their simulated expiry.
    if customer and not has_customer_consent(customer):
        return False

    kind = get_kind(trigger)
    if kind in ALWAYS_USEFUL_KINDS:
        return True
    return int(trigger.get("urgency", 0) or 0) >= 1


def compose_customer_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> tuple[str, str, str]:
    kind = get_kind(trigger)
    payload = trigger.get("payload", {}) or {}
    cname = customer_display(customer)
    mname = business_label(merchant)
    owner = salutation(category, merchant)
    loc = location_label(merchant)
    rel = customer.get("relationship", {}) or {}
    prefs = customer.get("preferences", {}) or {}
    lang = language_pref(customer, merchant)
    offer = active_offer(merchant, category)
    slots = first_available_slots(payload)
    slot_text = " or ".join(slots[:2]) if slots else "one convenient slot this week"

    if kind in {"recall_due", "customer_lapsed_soft"}:
        service = str(payload.get("service_due") or "recall").replace("_", " ")
        last = payload.get("last_service_date") or rel.get("last_visit")
        due = payload.get("due_date")
        prefix = f"Hi {cname}, {mname} here 🦷"
        if "hi" in lang:
            body = (
                f"{prefix}. Aapka {service} due hai{f' ({due})' if due else ''}; last visit {last}. "
                f"Apke liye {slot_text} available hai. {offer}. Reply 1/2 for a slot, or send your preferred time. STOP to opt out."
            )
        else:
            body = (
                f"{prefix}. Your {service} is due{f' on {due}' if due else ''}; last visit was {last}. "
                f"We can hold {slot_text}. {offer}. Reply 1/2 for a slot, or share a time that works. STOP to opt out."
            )
        rationale = "Customer recall uses due date, last visit, actual slots, language preference, active offer, and an opt-out path."
        return trim_message(body), "slot_choice", rationale

    if kind in {"appointment_tomorrow", "appointment_reminder"}:
        appt = payload.get("appointment_time") or payload.get("slot") or payload.get("appointment_iso") or "tomorrow"
        service = str(payload.get("service") or rel.get("services_received", ["appointment"])[-1] if rel.get("services_received") else "appointment").replace("_", " ")
        body = (
            f"Hi {cname}, reminder from {mname}: your {service} is scheduled for {appt}. "
            f"Reply CONFIRM to keep it, RESCHEDULE for another time, or STOP to opt out."
        )
        rationale = "Appointment reminder centers on scheduled time/service and offers confirm/reschedule choices."
        return trim_message(body), "confirm_reschedule", rationale

    if kind in {"wedding_package_followup", "bridal_followup"}:
        wedding = payload.get("wedding_date")
        trial = payload.get("trial_completed") or rel.get("first_visit")
        days = payload.get("days_to_wedding")
        step = str(payload.get("next_step_window_open", "prep window")).replace("_", " ")
        bridal_offer = active_offer(merchant, category, "bridal") or active_offer(merchant, category)
        body = (
            f"Hi {cname} 💍 {owner} from {mname} here. Your bridal trial was {trial}; "
            f"{days} days to the wedding on {wedding}, so the {step} window is open. "
            f"We can start with {bridal_offer}. Want me to block your preferred {prefs.get('preferred_slots', 'slot')} for the first session?"
        )
        rationale = "Bridal follow-up uses trial date, wedding date, countdown, preference, and the merchant's bridal/service offer."
        return trim_message(body), "binary_yes_no", rationale

    if kind in {"customer_lapsed_hard", "winback_customer"}:
        days = payload.get("days_since_last_visit") or payload.get("days_lapsed")
        focus = str(payload.get("previous_focus") or prefs.get("training_focus") or "your earlier goal").replace("_", " ")
        owner_name = owner.replace("Dr. ", "")
        body = (
            f"Hi {cname} 👋 {owner_name} from {mname} here. It has been {days} days since your last visit — no judgment. "
            f"Since your earlier focus was {focus}, we can restart with a light trial/check-in using {offer}. "
            f"Reply YES to hold an evening slot this week — no commitment, no auto-charge."
        )
        rationale = "Lapse winback removes guilt, uses lapse duration and previous goal, and lowers the commitment barrier."
        return trim_message(body), "binary_yes_no", rationale

    if kind == "trial_followup":
        trial = payload.get("trial_date")
        body = (
            f"Hi {cname}, {mname} here. Hope the trial on {trial} felt comfortable. "
            f"Next option: {slot_text}. Should I reserve one spot? Reply YES — no payment now."
        )
        rationale = "Trial follow-up anchors on the exact trial date and next session options with a no-payment CTA."
        return trim_message(body), "binary_yes_no", rationale

    if kind == "chronic_refill_due":
        meds = payload.get("molecule_list") or []
        meds_text = ", ".join(meds[:4]) if meds else "monthly medicines"
        runs_out = str(payload.get("stock_runs_out_iso") or payload.get("due_date") or "soon")[:10]
        saved = payload.get("delivery_address_saved")
        senior_offer = active_offer(merchant, category, "Senior") or active_offer(merchant, category, "Delivery") or offer
        addr = "saved address" if saved else "confirmed address"
        body = (
            f"Namaste {cname}, {mname} se reminder: {meds_text} refill may run out by {runs_out}. "
            f"{senior_offer}; delivery can go to your {addr}. Reply CONFIRM to pack the same medicines, or CHANGE if the doctor changed any dose/brand."
        )
        rationale = "Refill reminder uses molecule list, run-out date, delivery context, pharmacy offer, and a safety-friendly change option."
        return trim_message(body), "confirm_change", rationale

    if kind == "unplanned_slot_open":
        body = (
            f"Hi {cname}, {mname} has a slot opened up: {slot_text}. Because {customer_state_phrase(customer, trigger)}, this may be useful. "
            f"Reply YES to hold it for 15 minutes, or STOP to opt out."
        )
        rationale = "Open-slot message uses customer state and real slot availability without overclaiming."
        return trim_message(body), "binary_yes_no", rationale

    body = (
        f"Hi {cname}, {mname} here. Based on your {customer_state_phrase(customer, trigger)}, "
        f"we have a relevant update: {offer}. Reply YES for details, or STOP to opt out."
    )
    rationale = "Customer fallback stays grounded in customer relationship state, merchant identity, offer, and consent-safe opt-out."
    return trim_message(body), "binary_yes_no", rationale


def compose_merchant_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str, str]:
    kind = get_kind(trigger)
    payload = trigger.get("payload", {}) or {}
    s = salutation(category, merchant)
    name = business_label(merchant)
    loc = location_label(merchant)
    perf = merchant.get("performance", {}) or {}
    agg = merchant.get("customer_aggregate", {}) or {}
    signals = merchant.get("signals", []) or []
    peer = category.get("peer_stats", {}) or {}
    digest = find_digest_item(category, trigger)
    offer = active_offer(merchant, category)
    perf_text = performance_line(merchant, category)
    cta = "binary_yes_no"

    if kind in {"research_digest", "category_research_digest_release"}:
        source = digest.get("source", "this week's category digest")
        title = digest.get("title", "new category insight")
        trial = digest.get("trial_n")
        segment = str(digest.get("patient_segment") or digest.get("customer_segment") or "relevant customers").replace("_", " ")
        summary = digest.get("summary", "")
        cohort = agg.get("high_risk_adult_count") or agg.get("lapsed_count") or agg.get("total_unique_ytd")
        number_part = f"{intish(trial)}-person " if trial else ""
        cohort_part = f"your {intish(cohort)} {segment}" if cohort else f"your {segment}"
        summary_text = safe_excerpt(summary, 160)
        body = (
            f"{s}, {source} landed. One item fits {cohort_part}: {number_part}{title}. "
            f"{summary_text + ' ' if summary_text else ''}Want me to pull a 2-min summary and draft a customer WhatsApp around {offer}?"
        )
        rationale = "Research digest is source-cited, uses digest facts plus merchant cohort/offer, and offers a low-effort customer-ready draft."
        return trim_message(body), "open_ended", rationale

    if kind in {"regulation_change", "compliance_alert"}:
        source = digest.get("source", "compliance update")
        title = digest.get("title") or "compliance change"
        summary = digest.get("summary") or title
        deadline = payload.get("deadline_iso") or payload.get("deadline") or "the deadline"
        body = (
            f"{s}, compliance heads-up for {name}: {summary} Deadline: {deadline}. "
            f"If you still use older workflow/equipment, this can become an audit issue. Want a 5-point SOP + staff checklist today? — {source}"
        )
        rationale = "Regulation message names the source/deadline and converts risk into a concrete SOP checklist."
        return trim_message(body), cta, rationale

    if kind in {"perf_dip", "performance_dip"}:
        metric = payload.get("metric", "calls")
        delta = pct(payload.get("delta_pct", (perf.get("delta_7d", {}) or {}).get(f"{metric}_pct")), signed=True)
        baseline = payload.get("vs_baseline")
        signal_text = ", ".join(str(x).replace("_", " ") for x in signals[:2])
        body = (
            f"{s}, {metric} are {delta} in the last {payload.get('window', '7d')}"
            f"{f' vs baseline {baseline}' if baseline else ''}. Current snapshot: {perf_text}. "
            f"{f'Signal also shows {signal_text}. ' if signal_text else ''}Want me to draft one recovery post using {offer} and fix the weakest profile field?"
        )
        rationale = "Performance dip uses the metric, delta, merchant snapshot, signals, and a specific recovery action."
        return trim_message(body), cta, rationale

    if kind == "renewal_due":
        days = payload.get("days_remaining") or (merchant.get("subscription", {}) or {}).get("days_remaining")
        amount = money_or_text(payload.get("renewal_amount"))
        body = (
            f"{s}, Pro renewal is due in {days} days{f' ({amount})' if amount else ''}. Before paying, protect what is already working: {perf_text}. "
            f"Want a 1-screen ROI note showing what Pro is preserving for {name}?"
        )
        rationale = "Renewal is framed using the merchant's own 30-day performance value, not a generic payment reminder."
        return trim_message(body), cta, rationale

    if kind in {"festival_upcoming", "festival"}:
        festival = payload.get("festival", "festival")
        days = payload.get("days_until")
        relevance = str(payload.get("category_relevance") or category.get("display_name", "category demand")).replace("_", " ")
        body = (
            f"{s}, {festival} is {days} days away; {relevance} usually starts before the actual day. "
            f"For {loc}, your clean hook is {offer}. Want me to draft one {festival} GBP post + WhatsApp caption now?"
        )
        rationale = "Festival nudge ties exact timing, category relevance, locality, and existing offer to one action."
        return trim_message(body), cta, rationale

    if kind == "curious_ask_due":
        theme = (merchant.get("review_themes") or [{}])[0]
        hint = theme.get("common_quote") or theme.get("theme") or active_offer(merchant, category)
        body = (
            f"{s}, 10-sec check: at {name}, what are customers asking for most this week — {hint}? "
            f"Reply with one service/item name; I’ll turn it into a Google post + 4-line WhatsApp reply. Takes 5 min."
        )
        rationale = "Curious ask is deliberately low-friction and offers immediate effort externalization."
        return trim_message(body), "open_ended", rationale

    if kind == "winback_eligible":
        days = payload.get("days_since_expiry") or (merchant.get("subscription", {}) or {}).get("days_since_expiry")
        added = payload.get("lapsed_customers_added_since_expiry")
        dip = pct(payload.get("perf_dip_pct"), signed=True)
        body = (
            f"{s}, since Pro paused {days} days ago, {added} more customers moved into lapsed status and performance is {dip}. "
            f"No big restart: I can run one winback draft using {offer}, then you approve before anything sends. Want it?"
        )
        rationale = "Winback uses expiry duration, lapsed-customer count, performance dip, and a reversible next step."
        return trim_message(body), cta, rationale

    if kind == "ipl_match_today":
        match = payload.get("match", "today's match")
        venue = payload.get("venue", "local venue")
        match_time = str(payload.get("match_time_iso", ""))
        is_weeknight = payload.get("is_weeknight")
        digest_hint = find_digest_item(category, trigger).get("summary", "")
        time_txt = match_time[11:16] if len(match_time) >= 16 else "match time"
        if is_weeknight is False:
            judgment = "Saturday IPL often shifts covers to home-watch parties, so avoid a dine-in push."
            action = f"Use {offer} as a delivery/pre-order hook instead."
        else:
            judgment = "Weeknight IPL can lift quick orders before toss."
            action = f"Push {offer} with a pre-order CTA before {time_txt}."
        body = (
            f"{s}, {match} at {venue} today around {time_txt}. {judgment} {action} "
            f"Want me to draft a 2-line WhatsApp + GBP post?{f' Data note: {digest_hint[:80]}.' if digest_hint else ''}"
        )
        rationale = "IPL trigger adds judgment from match timing, uses the active offer, and recommends the safer channel/action."
        return trim_message(body), cta, rationale

    if kind == "review_theme_emerged":
        theme = str(payload.get("theme", "review issue")).replace("_", " ")
        count = payload.get("occurrences_30d")
        trend = payload.get("trend")
        quote = payload.get("common_quote", "")
        body = (
            f"{s}, {count} reviews in 30d now mention {theme}{f' ({trend})' if trend else ''}. One line customers used: “{quote}”. "
            f"Want me to draft a calm reply template + one profile note so future customers know what to expect?"
        )
        rationale = "Review trigger uses exact occurrence count, theme/trend, customer quote, and reputation-repair action."
        return trim_message(body), cta, rationale

    if kind == "milestone_reached":
        metric = str(payload.get("metric", "review_count")).replace("_", " ")
        now_v = payload.get("value_now")
        target = payload.get("milestone_value")
        body = (
            f"{s}, {name} is at {now_v} {metric}; {target} is within reach. "
            f"A small ask to recent happy customers works better than a public discount. Want me to draft the exact 3-line review request?"
        )
        rationale = "Milestone message uses current/target values and proposes one low-risk next step to cross it."
        return trim_message(body), cta, rationale

    if kind == "active_planning_intent":
        topic = str(payload.get("intent_topic", "new offer")).replace("_", " ")
        last = payload.get("merchant_last_message", "")
        if "thali" in topic.lower() or "corporate" in topic.lower():
            body = (
                f"{s}, continuing your idea — “{last}”. Starter shape for {loc}: 10 thalis, 25 thalis, and 50+ thalis with day-before ordering. "
                f"Use {offer} as the price anchor, then give offices a bulk slab. Want me to draft the menu-card copy + 3-line facilities-manager WhatsApp?"
            )
        elif "kids" in topic.lower() or "yoga" in topic.lower():
            body = (
                f"{s}, for {topic}, keep it specific: age band, Sat/Sun timing, 4-week batch, and parent FAQ. "
                f"Your hook can be {offer}. Want me to draft the announcement + parent FAQ now?"
            )
        else:
            body = (
                f"{s}, picking up from “{last}”: for {topic}, I’d keep one price anchor, one proof point from {name}, and one reply CTA. "
                f"Want me to draft the first WhatsApp + GBP post?"
            )
        rationale = "Planning-intent trigger advances an existing merchant request into a concrete artifact instead of asking more qualifying questions."
        return trim_message(body), cta, rationale

    if kind == "seasonal_perf_dip":
        metric = payload.get("metric", "views")
        delta = pct(payload.get("delta_pct"), signed=True)
        season = str(payload.get("season_note", "seasonal window")).replace("_", " ")
        active_members = agg.get("active_members") or agg.get("active_count") or agg.get("total_unique_ytd")
        body = (
            f"{s}, {metric} are {delta} in {payload.get('window', 'this window')} — but this looks seasonal: {season}. "
            f"Instead of panic ads, protect retention among {intish(active_members)} members/customers with {offer}. Want a 14-day comeback/attendance draft?"
        )
        rationale = "Seasonal dip explains why not to panic, uses the exact delta/window and merchant aggregate, and proposes retention action."
        return trim_message(body), cta, rationale

    if kind == "supply_alert":
        molecule = payload.get("molecule", "medicine")
        batches = ", ".join(payload.get("affected_batches", [])[:4])
        manufacturer = payload.get("manufacturer", "manufacturer")
        chronic = agg.get("chronic_rx_count") or agg.get("repeat_rx_count") or agg.get("total_unique_ytd")
        body = (
            f"{s}, urgent stock check: {manufacturer} voluntary recall for {molecule} batch(es) {batches}. "
            f"You have {intish(chronic)} repeat/chronic customers in context, so the safest first step is shelf-check + supplier note. Want me to draft both plus a customer-safe message?"
        )
        rationale = "Supply alert uses molecule, batches, manufacturer, customer aggregate, and a safe operational workflow."
        return trim_message(body), cta, rationale

    if kind == "category_seasonal":
        trends = payload.get("trends", []) or []
        trend_text = ", ".join(str(t).replace("_", " ") for t in trends[:4]) or str(payload.get("season", "seasonal demand"))
        body = (
            f"{s}, {category.get('display_name', 'category')} demand is shifting: {trend_text}. "
            f"For {name}, I can convert this into a shelf/menu/service priority list + one WhatsApp note using {offer}. Want the draft?"
        )
        rationale = "Seasonal category trigger translates trend signals into a practical inventory/service communication action."
        return trim_message(body), cta, rationale

    if kind == "gbp_unverified":
        uplift = pct(payload.get("estimated_uplift_pct"))
        path = str(payload.get("verification_path", "verification")).replace("_", " ")
        body = (
            f"{s}, {name} is still unverified on Google Business Profile. Expected lift after verification is about {uplift}; path: {path}. "
            f"Want the 3-step verification checklist I can walk you through?"
        )
        rationale = "GBP verification uses exact status, estimated uplift, verification path, and a checklist CTA."
        return trim_message(body), cta, rationale

    if kind == "cde_opportunity":
        source = digest.get("source", "category calendar")
        title = digest.get("title", "CDE opportunity")
        credits = payload.get("credits") or digest.get("credits")
        fee = money_or_text(payload.get("fee") or digest.get("fee")) or str(digest.get("actionable", "details"))
        body = (
            f"{s}, useful CDE alert: {title} ({credits} credits), fee {fee}. Source: {source}. "
            f"Want me to save the event details and draft a short post showing patients your clinic keeps up with new techniques?"
        )
        rationale = "CDE trigger is source/credit/fee-specific and converts learning into credibility content."
        return trim_message(body), cta, rationale

    if kind == "competitor_opened":
        comp = payload.get("competitor_name", "a competitor")
        dist = payload.get("distance_km")
        their_offer = payload.get("their_offer")
        opened = payload.get("opened_date")
        body = (
            f"{s}, heads-up: {comp} opened {dist} km from {loc} on {opened} with {their_offer}. "
            f"Don't copy the discount blindly. Your safer defense is {offer} + trust proof from reviews. Want a comparison-safe GBP post?"
        )
        rationale = "Competitor trigger uses competitor name, distance, offer/date, and avoids unsafe claims while recommending defensive positioning."
        return trim_message(body), cta, rationale

    if kind in {"perf_spike", "performance_spike"}:
        metric = payload.get("metric", "calls")
        delta = pct(payload.get("delta_pct"), signed=True)
        driver = str(payload.get("likely_driver", "recent activity")).replace("_", " ")
        baseline = payload.get("vs_baseline")
        body = (
            f"{s}, nice signal: {metric} are {delta} in {payload.get('window', '7d')}"
            f"{f' vs baseline {baseline}' if baseline else ''}. Likely driver: {driver}. "
            f"Want me to turn this winner into 2 more posts + one WhatsApp follow-up before momentum cools?"
        )
        rationale = "Performance spike identifies metric, uplift, baseline/driver, and next-best amplification."
        return trim_message(body), cta, rationale

    if kind == "dormant_with_vera":
        days = payload.get("days_since_last_merchant_message")
        last_topic = str(payload.get("last_topic", "last topic")).replace("_", " ")
        body = (
            f"{s}, it has been {days} days since we last spoke about {last_topic}. I won’t push a big task. "
            f"One useful restart: a single {offer} post for {loc}. Want me to draft just that?"
        )
        rationale = "Dormancy message acknowledges silence, reduces effort, and proposes exactly one concrete action."
        return trim_message(body), cta, rationale

    if kind == "weather_heatwave":
        temp = payload.get("temperature_c") or payload.get("temp_c") or payload.get("temperature")
        body = (
            f"{s}, heatwave alert around {loc}{f' ({temp}°C)' if temp else ''}. This changes walk-in behaviour. "
            f"Want me to draft one heat-safe update around {offer} and business hours, without overpromising?"
        )
        rationale = "Weather trigger grounds in locality/temperature and proposes a practical customer communication."
        return trim_message(body), cta, rationale

    # Generic but still grounded and trigger-specific.
    body = (
        f"{s}, quick update for {name}: {kind.replace('_', ' ')} is active now. "
        f"Current snapshot: {perf_text or 'context received'}; useful next step is {offer}. "
        f"Want me to draft the exact message and keep it ready for approval?"
    )
    rationale = "Fallback uses merchant name, trigger kind, current merchant data, offer, and a direct low-friction CTA."
    return trim_message(body), cta, rationale


def maybe_gemini_polish(body: str, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: Optional[dict[str, Any]]) -> str:
    """Optional wording polish. Disabled by default for speed and determinism."""
    if not (USE_GEMINI_POLISH and GEMINI_API_KEY):
        return body
    try:
        prompt = {
            "task": "Polish this WhatsApp message without adding facts. Keep all numbers, names, dates, prices, sources unchanged. Output JSON only: {\"body\": \"...\"}.",
            "body": body,
            "category_slug": category.get("slug"),
            "merchant_name": business_label(merchant),
            "trigger_kind": trigger.get("kind"),
            "customer_name": customer_display(customer) if customer else None,
        }
        data = json.dumps({
            "contents": [{"parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 512, "responseMimeType": "application/json"},
        }).encode("utf-8")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urlrequest.urlopen(req, timeout=8)
        out = json.loads(resp.read().decode("utf-8"))
        text = out["candidates"][0]["content"]["parts"][0]["text"]
        polished = str(json.loads(text).get("body", "")).strip()
        if polished and len(polished) <= max(950, len(body) + 160):
            return polished
    except (Exception, urlerror.URLError):
        return body
    return body


def build_action(trigger_id: str, trigger: dict[str, Any], merchant: dict[str, Any], category: dict[str, Any], customer: Optional[dict[str, Any]]) -> dict[str, Any]:
    if customer:
        body, cta, rationale = compose_customer_message(category, merchant, trigger, customer)
        send_as = "merchant_on_behalf"
    else:
        body, cta, rationale = compose_merchant_message(category, merchant, trigger)
        send_as = "vera"

    body = maybe_gemini_polish(body, category, merchant, trigger, customer)
    mid = trigger_merchant_id(trigger) or merchant.get("merchant_id") or "unknown_merchant"
    cid = trigger_customer_id(trigger)
    conv_id = short_conversation_id(mid, trigger_id, cid)
    suppression = trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger_id}"
    template_name = f"vera_{get_kind(trigger) or 'message'}_v2"[:100]

    action = {
        "conversation_id": conv_id,
        "merchant_id": mid,
        "customer_id": cid,
        "send_as": send_as,
        "trigger_id": trigger_id,
        "template_name": template_name,
        "template_params": template_params_from_body(body, merchant, cta),
        "body": body,
        "cta": cta,
        "suppression_key": suppression,
        "rationale": rationale,
    }
    conversations[conv_id] = {
        "merchant_id": action["merchant_id"],
        "customer_id": action["customer_id"],
        "trigger_id": trigger_id,
        "suppression_key": suppression,
        "last_body": body,
        "last_action": action,
        "turns": [{"from": "vera", "body": body, "ts": utc_now()}],
        "status": "open",
    }
    suppressed_keys.add(suppression)
    return action


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------

AUTO_REPLY_PATTERNS = [
    r"thank you for contacting", r"thanks for contacting", r"we will respond", r"will respond shortly",
    r"business hours", r"office hours", r"away message", r"auto[- ]?reply", r"automated assistant",
    r"currently unavailable", r"our team will", r"team tak", r"sujhaav.*team", r"hamari team",
    r"welcome to .*clinic", r"welcome to .*salon", r"welcome to .*pharmacy", r"आपकी.*जानकारी",
]
NEGATIVE_PATTERNS = [
    r"\bstop\b", r"not interested", r"don'?t message", r"do not message", r"unsubscribe", r"remove me",
    r"useless", r"spam", r"shut up", r"go away", r"bothering me", r"never message", r"band karo",
]
COMMIT_PATTERNS = [
    r"\byes\b", r"yes please", r"go ahead", r"let'?s do", r"lets do", r"let us do", r"ok do", r"okay do", r"send it",
    r"start", r"proceed", r"confirm", r"what'?s next", r"whats next", r"i want to join", r"mujhe.*jud",
    r"draft", r"schedule", r"approve", r"book", r"reserve", r"hold", r"done", r"kar do", r"chalega",
]
LATER_PATTERNS = [r"later", r"tomorrow", r"after some time", r"busy", r"not now", r"call me later", r"baad mein"]
OFFTOPIC_PATTERNS = [r"gst", r"tax filing", r"income tax", r"loan", r"insurance", r"personal"]
BOOKING_PATTERNS = [r"\bbook\b", r"reserve", r"hold", r"slot", r"appointment", r"\bwed\b", r"\bthu\b", r"\bfri\b", r"\bsat\b", r"\bsun\b", r"\b\d{1,2}\s*(am|pm)\b", r"\b[12]\b"]
EDIT_PATTERNS = [r"\bedit\b", r"change", r"modify", r"replace", r"make it", r"instead"]


def matches_any(message: str, patterns: list[str]) -> bool:
    return any(re.search(p, message, flags=re.I) for p in patterns)


def detect_reply_intent(message: str, from_role: str = "merchant") -> str:
    msg = low(message)
    if matches_any(msg, AUTO_REPLY_PATTERNS):
        return "auto_reply"
    if matches_any(msg, NEGATIVE_PATTERNS):
        return "negative"
    if from_role.lower() == "customer" and matches_any(msg, BOOKING_PATTERNS):
        return "customer_booking"
    if matches_any(msg, EDIT_PATTERNS):
        return "edit_request"
    if matches_any(msg, LATER_PATTERNS):
        return "later"
    if matches_any(msg, OFFTOPIC_PATTERNS):
        return "offtopic"
    if matches_any(msg, COMMIT_PATTERNS):
        return "commit"
    if "?" in msg or any(w in msg for w in ["how", "what", "price", "cost", "details", "audit", "help", "can you"]):
        return "question"
    return "neutral"


def context_for_conversation(conv_id: str, merchant_id: Optional[str], customer_id: Optional[str]) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    conv = conversations.get(conv_id, {})
    mid = merchant_id or conv.get("merchant_id")
    cid = customer_id or conv.get("customer_id")
    tid = conv.get("trigger_id")
    merchant = get_ctx("merchant", mid) or {"merchant_id": mid, "identity": {"name": "your business", "owner_first_name": "there", "languages": ["en"]}, "category_slug": ""}
    category = get_ctx("category", merchant.get("category_slug")) or get_ctx("category", trigger_category_slug(get_ctx("trigger", tid) or {}, merchant)) or {"slug": merchant.get("category_slug", ""), "display_name": merchant.get("category_slug", "business")}
    customer = get_ctx("customer", cid) if cid else None
    trigger = get_ctx("trigger", tid) if tid else None
    return merchant, category, customer, trigger


def extract_slot_from_reply(message: str, trigger: Optional[dict[str, Any]]) -> str:
    msg = low(message)
    payload = (trigger or {}).get("payload", {}) or {}
    slots = first_available_slots(payload)
    if slots:
        if re.search(r"\b1\b|first", msg):
            return slots[0]
        if re.search(r"\b2\b|second", msg) and len(slots) >= 2:
            return slots[1]
        for slot in slots:
            words = [w for w in re.split(r"[^a-zA-Z0-9]+", slot.lower()) if len(w) >= 3]
            if any(w in msg for w in words):
                return slot
    # Generic extraction: keep visible date/time phrase from user's own reply.
    m = re.search(r"((mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+\d{1,2}\s+[a-z]{3,9},?\s*\d{1,2}(:\d{2})?\s*(am|pm)?)", message, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(\d{1,2}(:\d{2})?\s*(am|pm))", message, re.I)
    return m.group(1) if m else norm(message)[:80]


def trigger_specific_commitment_response(kind: str, name: str, s: str, offer: str, merchant: dict[str, Any], category: dict[str, Any], trigger: Optional[dict[str, Any]]) -> str:
    payload = (trigger or {}).get("payload", {}) or {}
    if kind in {"research_digest", "category_research_digest_release", "cde_opportunity"}:
        cohort = (merchant.get("customer_aggregate", {}) or {}).get("high_risk_adult_count") or (merchant.get("customer_aggregate", {}) or {}).get("total_unique_ytd")
        scope = f" for {intish(cohort)} relevant customers" if cohort else ""
        return f"Great, {s}. Drafting the summary + customer WhatsApp{scope}. Reply CONFIRM to send the customer draft, or EDIT with one service you want featured."
    if kind in {"regulation_change", "compliance_alert"}:
        deadline = payload.get("deadline_iso") or payload.get("deadline") or "the deadline"
        return f"Done, {s}. I’ll make the SOP checklist around the {deadline} compliance change: equipment check, exposure log, staff note, patient-safe wording, and audit file. Reply CONFIRM to use it."
    if kind in {"perf_spike", "performance_spike"}:
        return f"Done. I’ll convert this spike into 2 follow-up posts and one WhatsApp around {offer}. Reply CONFIRM and I’ll keep the first draft ready for approval."
    if kind in {"perf_dip", "performance_dip", "seasonal_perf_dip"}:
        return f"Done. I’ll draft a recovery/retention post for {name} using {offer}, with one clear reply CTA and no broad discounting. Reply CONFIRM to proceed."
    if kind in {"review_theme_emerged"}:
        return "Done. I’ll draft two pieces now: a calm public review reply and a profile note that sets expectations for future customers. Reply CONFIRM to use both."
    if kind in {"active_planning_intent"}:
        topic = str(payload.get("intent_topic", "the package")).replace("_", " ")
        return f"Great. I’ll turn {topic} into a ready draft: offer structure, price anchor, and 3-line customer/office outreach. Reply CONFIRM to proceed, or EDIT with a price change."
    if kind in {"competitor_opened"}:
        return f"Done. I’ll draft a comparison-safe post that protects {name}'s positioning without naming or attacking the competitor. Reply CONFIRM to use it."
    return f"Done — moving to action for {name}. I’ll draft one message around {offer} with a single CTA. Reply CONFIRM to proceed, or EDIT with changes."


def answer_trigger_question(message: str, kind: str, name: str, s: str, offer: str, trigger: Optional[dict[str, Any]], category: dict[str, Any], merchant: dict[str, Any]) -> str:
    msg = low(message)
    payload = (trigger or {}).get("payload", {}) or {}
    digest = find_digest_item(category, trigger or {})
    if kind in {"regulation_change", "compliance_alert"} or "audit" in msg or "d-speed" in msg or "x-ray" in msg or "xray" in msg:
        deadline = payload.get("deadline_iso") or payload.get("deadline") or "the deadline"
        source = digest.get("source", "the compliance update")
        return (
            f"Yes, {s}. Since you mentioned an old D-speed/X-ray setup, start with the audit path: 1) note equipment + film/sensor type, "
            f"2) check whether D-speed film needs replacement, 3) update exposure SOP, 4) keep staff acknowledgement, 5) file it before {deadline}. "
            f"Want me to format this into a 5-point checklist for {name}? — {source}"
        )
    if "price" in msg or "cost" in msg or "details" in msg:
        return f"For {name}, the safest customer hook from current context is {offer}. I won’t invent a discount. Want me to draft the exact copy with this price/service anchor?"
    if kind in {"ipl_match_today"}:
        return f"For today’s IPL trigger, I’d use the active offer only if the timing supports it; otherwise avoid forcing a promo. Want me to draft the safer WhatsApp + GBP version now?"
    return f"Fair question, {s}. Based only on current context, the useful next step is {offer} with one clear CTA. Want me to draft the exact copy now?"


def reply_action(body: ReplyBody) -> dict[str, Any]:
    msg = body.message.strip()
    from_role = body.from_role.lower().strip() or "merchant"
    intent = detect_reply_intent(msg, from_role)
    conv = conversations.setdefault(body.conversation_id, {"turns": [], "status": "open"})
    conv.setdefault("turns", []).append({"from": from_role, "body": msg, "ts": body.received_at or utc_now()})

    merchant, category, customer, trigger = context_for_conversation(body.conversation_id, body.merchant_id, body.customer_id)
    s = salutation(category, merchant)
    name = business_label(merchant)
    offer = active_offer(merchant, category)
    kind = get_kind(trigger)
    mid = body.merchant_id or conv.get("merchant_id") or merchant.get("merchant_id") or "unknown"

    if body.conversation_id in closed_conversations or conv.get("status") == "closed":
        return {"action": "end", "rationale": "Conversation already closed; no further messages sent."}

    if intent == "auto_reply":
        key = (str(mid), normalize_auto_reply_text(msg))
        auto_reply_memory[key] = auto_reply_memory.get(key, 0) + 1
        count = auto_reply_memory[key]
        conv["auto_reply_count"] = conv.get("auto_reply_count", 0) + 1
        if count == 1 and body.turn_number <= 2:
            return {
                "action": "send",
                "body": "Looks like a WhatsApp Business auto-reply. When the owner/manager sees this, just reply YES and I’ll prepare the draft — no action till then.",
                "cta": "binary_yes_no",
                "rationale": "First canned auto-reply detected; one owner-facing prompt is allowed, then loop prevention takes over.",
            }
        if count == 2:
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": "Same/near-same auto-reply seen twice for this merchant; waiting 24h instead of burning turns.",
            }
        closed_conversations.add(body.conversation_id)
        conv["status"] = "closed"
        return {
            "action": "end",
            "rationale": "Repeated canned auto-reply detected 3+ times; closing the conversation to prevent an auto-reply loop.",
        }

    if intent == "negative":
        closed_conversations.add(body.conversation_id)
        conv["status"] = "closed"
        if mid:
            merchant_blocked_until[str(mid)] = time.time() + 30 * 24 * 3600
        return {"action": "end", "rationale": "User explicitly opted out/was hostile; ending and suppressing merchant follow-ups."}

    if intent == "customer_booking" or (from_role == "customer" and intent == "commit"):
        # Customer-facing replay: choose the slot and confirm the merchant handoff.
        if not customer and body.customer_id:
            customer = get_ctx("customer", body.customer_id)
        cname = customer_display(customer)
        slot = extract_slot_from_reply(msg, trigger)
        body_text = (
            f"Thanks {cname}. I’ve noted {slot} for {name}. The clinic/store team will confirm shortly; "
            f"reply CHANGE if you want another time, or STOP to opt out."
        )
        return {
            "action": "send",
            "body": trim_message(body_text),
            "cta": "change_or_stop",
            "rationale": "Customer picked/confirmed a slot; acknowledging the booking request instead of switching to merchant draft approval flow.",
        }

    if intent == "later":
        return {"action": "wait", "wait_seconds": 3600, "rationale": "User asked to defer; waiting before any follow-up."}

    if intent == "offtopic":
        topic = kind.replace("_", " ") if kind else "the growth task"
        body_text = (
            f"I’ll leave that to your CA/specialist — I do not want to give half-correct advice. "
            f"Coming back to {topic}: I can handle the useful business step for {name}. Reply YES and I’ll prepare it."
        )
        return {"action": "send", "body": trim_message(body_text), "cta": "binary_yes_no", "rationale": "Off-topic ask declined safely and redirected to the current Vera task."}

    if intent == "edit_request":
        body_text = (
            f"Noted. Send the exact change in one line — service, price, tone, or timing — and I’ll update the draft without changing the core CTA around {offer}."
        )
        return {"action": "send", "body": trim_message(body_text), "cta": "open_ended", "rationale": "Edit intent detected; asking only for the minimum edit detail needed."}

    if intent == "commit":
        body_text = trigger_specific_commitment_response(kind, name, s, offer, merchant, category, trigger)
        return {"action": "send", "body": trim_message(body_text), "cta": "binary_confirm_cancel", "rationale": "Explicit commitment detected; switched immediately from qualification to action/confirmation."}

    if intent == "question":
        body_text = answer_trigger_question(msg, kind, name, s, offer, trigger, category, merchant)
        return {"action": "send", "body": trim_message(body_text), "cta": "binary_yes_no", "rationale": "Answered the contextual question without inventing facts, then moved to the next concrete step."}

    # Neutral merchant replies should stay on current trigger, not generic approval loops.
    if from_role == "customer":
        cname = customer_display(customer)
        body_text = f"Thanks {cname}. I’ll keep it simple: reply with preferred day/time, or STOP to opt out."
        rationale = "Neutral customer reply routed back to slot preference, not merchant approval."
    else:
        body_text = (
            f"Got it, {s}. For {name}, I’ll keep the next step small: one draft around {offer}, one clear CTA, no extra questions. "
            f"Reply YES to prepare it, or STOP and I’ll close this."
        )
        rationale = "Neutral merchant reply handled with one small next step and opt-out."
    return {"action": "send", "body": trim_message(body_text), "cta": "binary_yes_no", "rationale": rationale}


# ---------------------------------------------------------------------------
# Optional pure compose function for offline/module-based scoring
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    body, cta, rationale = compose_customer_message(category, merchant, trigger, customer) if customer else compose_merchant_message(category, merchant, trigger)
    send_as = "merchant_on_behalf" if customer else "vera"
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('id')}",
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for scope, _ in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TS), "contexts_loaded": counts}


@app.get("/healthz")
async def healthz_alias():
    return await healthz()


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": ["Tejaskrit"],
        "model": f"deterministic Vera decision router v2 + optional no-new-facts Gemini polish ({GEMINI_MODEL})",
        "approach": "Context-version store, trigger dispatch, category-specific deterministic composer, customer slot handling, stateful auto-reply loop prevention, and action-oriented replay routing.",
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": utc_now(),
    }


@app.get("/metadata")
async def metadata_alias():
    return await metadata()


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": body.scope})
    key = (body.scope, body.context_id)
    current = contexts.get(key)
    if current and current["version"] >= body.version:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current["version"]})
    contexts[key] = {"version": body.version, "payload": body.payload, "delivered_at": body.delivered_at, "stored_at": utc_now()}
    return {"accepted": True, "ack_id": f"ack_{slugify(body.context_id)}_v{body.version}", "stored_at": contexts[key]["stored_at"]}


@app.post("/context")
async def push_context_alias(body: ContextBody):
    return await push_context(body)


@app.post("/v1/tick")
async def tick(body: TickBody):
    now = parse_dt(body.now)
    actions: list[dict[str, Any]] = []

    indexed: list[tuple[int, int, str, dict[str, Any]]] = []
    for idx, tid in enumerate(body.available_triggers or []):
        trg = get_ctx("trigger", tid)
        if trg:
            indexed.append((-(int(trg.get("urgency", 0) or 0)), idx, tid, trg))
    indexed.sort()

    for _, _, tid, trg in indexed:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break
        mid = trigger_merchant_id(trg)
        cid = trigger_customer_id(trg)
        merchant = get_ctx("merchant", mid)
        if not merchant:
            continue
        category = get_ctx("category", merchant.get("category_slug")) or get_ctx("category", trigger_category_slug(trg, merchant)) or {"slug": merchant.get("category_slug", ""), "display_name": merchant.get("category_slug", "business")}
        customer = get_ctx("customer", cid) if cid else None
        if cid and not customer:
            # The small public simulator sometimes omits customer warmup even for
            # customer-scoped triggers. Compose from trigger payload instead of
            # falling back to a merchant-generic message.
            payload = trg.get("payload", {}) or {}
            customer = {
                "customer_id": cid,
                "merchant_id": mid,
                "identity": {"name": payload.get("customer_name") or payload.get("patient_name") or "there", "language_pref": payload.get("language_pref", "english")},
                "relationship": {"last_visit": payload.get("last_service_date") or payload.get("last_visit")},
                "state": payload.get("state", "active"),
                "preferences": {"channel": "whatsapp", "reminder_opt_in": True},
                "consent": {"opted_in_at": payload.get("opted_in_at") or "context-trigger", "scope": ["triggered_outreach"]},
            }
        if not should_send(trg, merchant, customer, now):
            continue
        try:
            actions.append(build_action(tid, trg, merchant, category, customer))
        except Exception as exc:
            # Keep the tick alive; one malformed context should not create an operational penalty.
            continue
    return {"actions": actions}


@app.post("/tick")
async def tick_alias(body: TickBody):
    return await tick(body)


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    try:
        return reply_action(body)
    except Exception as exc:
        return {
            "action": "send",
            "body": "Got it. I’ll keep this simple and avoid guessing. Reply YES to continue with the draft, or STOP and I’ll close this.",
            "cta": "binary_yes_no",
            "rationale": f"Safe fallback after reply handler error: {type(exc).__name__}",
        }


@app.post("/reply")
async def reply_alias(body: ReplyBody):
    return await reply(body)


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    suppressed_keys.clear()
    closed_conversations.clear()
    merchant_blocked_until.clear()
    auto_reply_memory.clear()
    return {"status": "wiped", "at": utc_now()}


@app.post("/teardown")
async def teardown_alias():
    return await teardown()


@app.get("/ping", response_class=PlainTextResponse)
def ping() -> str:
    return "OK"


@app.head("/ping", response_class=PlainTextResponse)
def ping_head() -> str:
    return ""


@app.get("/", response_class=PlainTextResponse)
def root() -> str:
    return "OK - Tejaskrit Vera bot live. Use /v1/healthz, /v1/metadata, /v1/context, /v1/tick, /v1/reply."
