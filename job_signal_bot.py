import os
import re
import json
import html as html_mod
import base64
import asyncio
from datetime import datetime, timezone

import discord
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# Poll interval
POLL_SECONDS = 90

#store message ids already processed
STATE_FILE = "state_jobright.json"

GMAIL_TOKEN_FILE = "token.json"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed_message_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def gmail_service():
    """
    Uses existing token.json. If token is invalid/expired and no refresh is possible,
    Google libs will error and you'll re-run your auth script to regenerate token.json.
    """
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE)
    return build("gmail", "v1", credentials=creds)


def extract_html_from_gmail_message(msg: dict) -> str | None:
    """
    Tries to find the text/html part in a Gmail message resource.
    """
    payload = msg.get("payload", {})
    parts = payload.get("parts")

    def decode_body(body_data: str) -> str:
        raw_bytes = base64.urlsafe_b64decode(body_data.encode("utf-8"))
        return raw_bytes.decode("utf-8", errors="replace")

    # Some emails are single-part
    body = payload.get("body", {})
    data = body.get("data")
    mime = payload.get("mimeType", "")

    if mime == "text/html" and data:
        return decode_body(data)

    # Multi-part emails
    if parts:
        stack = list(parts)
        while stack:
            p = stack.pop(0)
            mime_type = p.get("mimeType", "")
            body_data = p.get("body", {}).get("data")

            if mime_type == "text/html" and body_data:
                return decode_body(body_data)

            # nested parts
            if p.get("parts"):
                stack.extend(p["parts"])

    return None


def build_jobright_messages_from_html(html_text: str) -> list[str]:
    """
    Parses a Jobright digest email HTML and returns a list of Discord-ready messages (one per job).
    """
    raw = html_mod.unescape(html_text)
    soup = BeautifulSoup(raw, "lxml")

    # job links in order
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "jobright.ai/jobs/info/" in href and href not in seen:
            seen.add(href)
            links.append(href)

    text = clean(soup.get_text(" ", strip=True))
    marker = "Explore this today's top matches, curated to align with your preferences, experiences, and skill sets."
    if marker in text:
        text = text.split(marker, 1)[1].strip()

    blocks = [clean(p) for p in text.split("APPLY NOW") if clean(p)]
    if blocks and blocks[-1].lower().startswith("view more opportunities"):
        blocks = blocks[:-1]

    n = min(len(links), len(blocks))
    links = links[:n]
    blocks = blocks[:n]

    messages = []
    for b, link in zip(blocks, links):
        # match %
        m_match = re.search(r"\b(\d{1,3})%\b", b)
        match_pct = m_match.group(1) if m_match else ""

        # pay
        m_pay = re.search(r"(\$\d[\d,]*\s*(?:K)?\/(?:yr|hr)\s*-\s*\$\d[\d,]*\s*(?:K)?\/(?:yr|hr))", b)
        pay = m_pay.group(1) if m_pay else ""

        # location
        location = "Remote" if re.search(r"\bRemote\b", b) else ""
        if not location:
            m_city = re.search(r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b", b)
            if m_city:
                location = m_city.group(1)

        # company: first chunk before the first " · "
        company = b.split(" · ", 1)[0].strip()

        # role: after "<match>%", then trim
        after = b
        if match_pct and (match_pct + "%") in after:
            after = after.split(match_pct + "%", 1)[1]
        after = clean(after)

        # cut off after known terminators
        terminators = [
            pay,
            location,
            "referrals",
            "hour ago",
            "hours ago",
            "day ago",
            "days ago",
            "Be an early applicant",
        ]
        for t in terminators:
            if t and t in after:
                after = after.split(t, 1)[0]

        after = after.strip(" ·-")

        # remove descriptor noise that precedes the role
        noise = [
            "Public Company",
            "Growth Stage",
            "Late Stage",
            "Early Stage",
            "Consulting",
            "Finance",
            "Digital Media",
            "Telecom & Communications",
            "Artificial Intelligence (AI)",
            "Computer Software",
            "Advertising",
        ]
        for nword in noise:
            after = after.replace(nword, "")

        role = clean(after)

        # If company name leaked into role, strip it
        if company and role.lower().startswith(company.lower()):
            role = clean(role[len(company):])

        msg_lines = []
        msg_lines.append("🔔")
        msg_lines.append(role if role else "(title not found)")
        msg_lines.append(f"📍 {location}" if location else "📍 (location not found)")
        msg_lines.append(f"- company: {company}")
        if match_pct:
            msg_lines.append(f"- match: {match_pct}%")
        if pay:
            msg_lines.append(f"- pay: {pay}")
        msg_lines.append("- source: Jobright")
        msg_lines.append(f"- link: [Job link]({link})")

        msg = "\n".join(msg_lines)

        # Discord limit
        if len(msg) > 2000:
            msg = msg[:1990] + "\n..."

        messages.append(msg)

    return messages


def find_new_jobright_message_ids(service, already_processed: set[str], max_results: int = 5) -> list[str]:
    """
    Finds recent Jobright emails.
    You can tighten/expand the query later. This is a sane starting point.
    """
    query = "from:jobright.ai OR from:noreply@jobright.ai newer_than:7d"
    resp = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results
    ).execute()

    msg_ids = [m["id"] for m in resp.get("messages", [])]
    # keep only ones we haven't posted yet
    return [mid for mid in msg_ids if mid not in already_processed]


async def post_messages(channel: discord.TextChannel, messages: list[str]) -> None:
    for i, msg in enumerate(messages, start=1):
        await channel.send(msg)
        await asyncio.sleep(1.2)  # rate limit friendly


async def poll_loop(channel: discord.TextChannel):
    state = load_state()
    processed = set(state.get("processed_message_ids", []))

    service = gmail_service()
    print("Gmail service ready.")
    print(f"Polling every {POLL_SECONDS} seconds...")

    while True:
        try:
            new_ids = find_new_jobright_message_ids(service, processed, max_results=10)

            # Oldest-first posting
            new_ids.reverse()

            if new_ids:
                print(f"Found {len(new_ids)} new Jobright email(s).")

            for mid in new_ids:
                msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
                html_part = extract_html_from_gmail_message(msg)

                if not html_part:
                    print(f"Skipping {mid}: no HTML part found.")
                    processed.add(mid)
                    continue

                job_messages = build_jobright_messages_from_html(html_part)

                if job_messages:
                    await post_messages(channel, job_messages)
                    print(f"Posted {len(job_messages)} job(s) from email {mid}.")
                else:
                    print(f"No jobs parsed from email {mid}.")

                processed.add(mid)

                
                state["processed_message_ids"] = list(processed)[-5000:]  
                save_state(state)

        except Exception as e:
            
            print("Poll error:", repr(e))

        await asyncio.sleep(POLL_SECONDS)


async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN missing in .env")
    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID missing in .env")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        channel = client.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            print("Could not find channel. Check DISCORD_CHANNEL_ID and bot permissions.")
            await client.close()
            return

        print(f"Logged in as {client.user}. Starting poll loop...")
        await poll_loop(channel)

    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())