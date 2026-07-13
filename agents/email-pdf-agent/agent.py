#!/usr/bin/env python3
"""Sample agent: find mail from a sender, save the PDF attachments published
in a given year. See README.md for how the agent loop works.
"""

import argparse
import base64
import io
import json
import os
import re
from datetime import datetime

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pypdf import PdfReader

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, "token.json")
CREDENTIALS_PATH = os.path.join(HERE, "credentials.json")

MODEL = "claude-sonnet-5"


def gmail_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Tools. Each one is a plain Python function; the model only ever sees the
# JSON schema below and the JSON-serializable value each function returns.
# ---------------------------------------------------------------------------

class GmailTools:
    def __init__(self, service, out_dir):
        self.service = service
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.log = []

    def search_gmail(self, query, max_results=25):
        resp = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        results = []
        for m in resp.get("messages", []):
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=m["id"], format="metadata",
                     metadataHeaders=["Subject", "From", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            results.append(
                {
                    "message_id": m["id"],
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "has_attachment": self._has_attachment(msg["payload"]),
                }
            )
        return {"count": len(results), "messages": results}

    def list_attachments(self, message_id):
        msg = self.service.users().messages().get(userId="me", id=message_id).execute()
        attachments = []
        for part in self._iter_parts(msg["payload"]):
            filename = part.get("filename")
            body = part.get("body", {})
            if filename and body.get("attachmentId"):
                attachments.append(
                    {
                        "attachment_id": body["attachmentId"],
                        "filename": filename,
                        "mime_type": part.get("mimeType", ""),
                        "size_bytes": body.get("size", 0),
                    }
                )
        return {"message_id": message_id, "attachments": attachments}

    def inspect_pdf(self, message_id, attachment_id, filename):
        data = self._fetch_attachment_bytes(message_id, attachment_id)
        info = {"filename": filename, "size_bytes": len(data)}
        try:
            reader = PdfReader(io.BytesIO(data))
            meta = reader.metadata or {}
            info["title"] = meta.get("/Title")
            info["num_pages"] = len(reader.pages)
            created = meta.get("/CreationDate")
            info["publication_year"] = self._year_from_pdf_date(created)
        except Exception as e:
            info["error"] = f"could not parse PDF: {e}"
            info["publication_year"] = None
        self.log.append(("inspect_pdf", filename, info.get("publication_year")))
        return info

    def save_attachment(self, message_id, attachment_id, filename):
        data = self._fetch_attachment_bytes(message_id, attachment_id)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        path = os.path.join(self.out_dir, safe_name)
        with open(path, "wb") as f:
            f.write(data)
        self.log.append(("save_attachment", filename, path))
        return {"saved_path": path, "size_bytes": len(data)}

    # -- helpers -----------------------------------------------------------

    def _fetch_attachment_bytes(self, message_id, attachment_id):
        att = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(att["data"])

    def _iter_parts(self, payload):
        if "parts" in payload:
            for part in payload["parts"]:
                yield from self._iter_parts(part)
        else:
            yield payload

    def _has_attachment(self, payload):
        return any(p.get("filename") for p in self._iter_parts(payload))

    @staticmethod
    def _year_from_pdf_date(pdf_date):
        # PDF /CreationDate looks like "D:20260315120000+05'30'"
        if not pdf_date:
            return None
        match = re.search(r"D:(\d{4})", pdf_date)
        return int(match.group(1)) if match else None


TOOL_SCHEMAS = [
    {
        "name": "search_gmail",
        "description": "Search Gmail using standard Gmail search syntax (from:, subject:, has:attachment, etc).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_attachments",
        "description": "List the attachments (filename, mime type, size, attachment_id) on a Gmail message.",
        "input_schema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
    {
        "name": "inspect_pdf",
        "description": (
            "Download a PDF attachment just far enough to read its metadata "
            "(title, page count, publication_year from /CreationDate). Does NOT "
            "save it to disk. Use this to decide whether a PDF matches the "
            "target year before calling save_attachment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": ["message_id", "attachment_id", "filename"],
        },
    },
    {
        "name": "save_attachment",
        "description": "Persist a PDF attachment to the local output directory. Only call this for PDFs you've decided to keep.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": ["message_id", "attachment_id", "filename"],
        },
    },
]


def run_agent(query, year, out_dir):
    service = gmail_service()
    tools = GmailTools(service, out_dir)
    client = anthropic.Anthropic()

    system = (
        "You are an email-triage agent. Use search_gmail to find messages "
        f"matching the user's query. For every PDF attachment you find, call "
        f"inspect_pdf to check its publication_year. Only call save_attachment "
        f"for PDFs whose publication_year is exactly {year}; explicitly skip "
        "everything else and say why. When you're done, give a short summary: "
        "how many messages you checked, how many PDFs you saved, how many you "
        "skipped and why."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f'Find mail matching this Gmail query: {query!r}. '
                f"Download every PDF attachment published in {year}."
            ),
        }
    ]

    dispatch = {
        "search_gmail": tools.search_gmail,
        "list_attachments": tools.list_attachments,
        "inspect_pdf": tools.inspect_pdf,
        "save_attachment": tools.save_attachment,
    }

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            final_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            print(final_text)
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"[agent] calling {block.name}({json.dumps(block.input)})")
            result = dispatch[block.name](**block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )
        messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help='Gmail search query, e.g. from:"CMD office"')
    parser.add_argument("--year", type=int, required=True, help="Publication year to keep, e.g. 2026")
    parser.add_argument("--out", default="./downloads", help="Local folder to save matching PDFs into")
    args = parser.parse_args()
    run_agent(args.query, args.year, args.out)
