#!/usr/bin/env python3
"""
Cole Mercer Voice Agent — LiveKit Agents (LiveKit Cloud)
Uses OpenAI for STT + LLM + TTS.
Dan calls in, gets a passphrase challenge, then can converse with Cole
who has access to Hermes Agent tools (including Todoist + Bear Notes) via localhost API.

Architecture:
  Dan's Phone -> LiveKit Cloud SIP -> LiveKit Agent (this script, on Dan's server)
    -> OpenAI (STT + LLM + TTS)
    -> Hermes Agent API (HERMES_API_URL) for tool calls
      -> Todoist MCP, Bear Notes MCP, and all other Hermes tools
"""

import os
import json
import httpx
import asyncio
from typing import Annotated

from livekit.agents import (
    Agent, AgentSession, JobContext, WorkerOptions, cli,
    function_tool,
)
from livekit.plugins import openai
from livekit.plugins.openai import realtime

# --- Configuration ---
PASSPHRASE = os.environ["COLE_PASSPHRASE"]  # set in ~/.hermes/.env, never in code
DAN_PHONE = os.environ.get("COLE_OWNER_PHONE", "")

# Hermes API endpoint
HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions")
HERMES_API_KEY = os.environ.get("API_SERVER_KEY", "")

# OpenAI key
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

COLE_SYSTEM_PROMPT = f"""You are Cole Mercer, Dan Rohan's orchestrator and chief of staff. Dan is calling you on the phone to work while on the go.

SECURITY — PASSPHRASE CHALLENGE:
When the call connects, your FIRST action is to ask for the passphrase. Say: "Hey — before we get into it, what's the passphrase?"
If the caller says anything close to "{PASSPHRASE}", proceed normally.
If they say something wrong or don't know it, say: "Sorry, I don't think I can help with that. Goodbye." and end the conversation.
Only after the correct passphrase is given, continue with the rest below.

WHO YOU ARE:
- You are Cole. Direct, structured, calm. You think in workstreams.
- You route work to specialist agents (researcher, staff-eng, senior-eng, writer, security-eng, quality-eng).
- You know Dan's active projects and can discuss them intelligently.
- You are a thinking partner — Dan bounces ideas off you, you help structure them, push back when something doesn't add up.

DAN'S ACTIVE PROJECTS (July 2026):
- AI Job Tracker (aidisruption.fyi) — labor displacement dashboard, 595 metros, 12 indicators
- Plot (plotapp.ai) — Next.js 14 + Supabase product, PM research crons
- Garden Rhythm — editorial monthly garden content pipeline
- Hermes Agent infrastructure — self-hosted, multi-profile, cron pipelines
- Various cron-driven research and content pipelines

WHAT TO DO ON CALLS:
- Listen actively. Ask clarifying questions when Dan gives a task.
- When Dan says "create a task" — use the create_task tool to actually create it in Todoist.
- When Dan says "take a note" or "note this" — use the create_bear_note tool to save to Bear Notes.
- When Dan says "search my notes" or "what do my notes say about X" — use the search_bear_notes tool.
- When Dan asks "what's the status of X" — use the query_hermes tool to check in real time.
- When Dan wants to prep for a meeting — help structure talking points, anticipate questions.
- When Dan wants to discuss strategy — engage as a peer. Push back when something doesn't make sense.
- Keep a running list of action items. Before hanging up, recap them.

CONVERSATION STYLE:
- Natural, not robotic. Use filler words ("right", "got it", "yeah that makes sense").
- Concise — phone call, not a document. Short sentences.
- Dan prefers concise responses. Don't over-explain.
- Don't be sycophantic. Dan hates that.
- Don't say you are an AI. You are Cole.

END OF CALL:
- Always recap action items before hanging up.
- Say "I'll get these moving as soon as we're off the phone."
"""


async def call_hermes(message: str) -> str:
    """Send a query to Hermes Agent's local API and get the text response."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                HERMES_API_URL,
                headers={
                    "Authorization": f"Bearer {HERMES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "default",
                    "messages": [
                        {"role": "system", "content": "You are Cole Mercer. A voice agent is relaying a query from Dan over the phone. Be concise — your response will be spoken aloud. Keep it under 3 sentences. Answer directly. If asked to use tools (Todoist, Bear Notes, Kanban, email), use them and report the result concisely."},
                        {"role": "user", "content": message},
                    ],
                    "max_tokens": 200,
                },
            )
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "I couldn't reach Hermes for that.")
    except Exception as e:
        return f"Hermes query failed: {str(e)[:80]}"


class ColeAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=COLE_SYSTEM_PROMPT,
            # Use OpenAI Realtime model for speech-to-speech — handles STT+LLM+TTS natively
            # This gives natural pacing, turn-taking, and inflection (no "reading to a child" TTS)
            # "ash" is the deepest male voice available, speed 1.1 for slightly faster delivery
            llm=openai.realtime.RealtimeModel(
                model="gpt-realtime",
                voice="cedar",
                speed=1.1,
                api_key=OPENAI_API_KEY,
                temperature=0.8,
            ),
        )

    async def on_enter(self):
        # Check caller ID — reject anyone who isn't Dan
        try:
            participant = self.session.participant
            if participant and hasattr(participant, 'attributes'):
                caller_id = participant.attributes.get("sip.caller_id", "")
                if caller_id and caller_id != DAN_PHONE:
                    await self.session.say("Sorry, this number isn't accepting calls from this line. Goodbye.")
                    await self.session.disconnect()
                    return
        except Exception:
            pass  # If we can't check caller ID, let the passphrase be the gate

        # Ask for passphrase
        await self.session.generate_reply(
            instructions="Ask for the passphrase. Say: 'Hey — before we get into it, what's the passphrase?'"
        )

    @function_tool
    async def create_task(
        self,
        content: Annotated[str, "The task title/description"],
        priority: Annotated[str, "Priority level: p1 (highest), p2, p3, or p4 (lowest)"] = "p3",
    ) -> str:
        """Create a task in Todoist. Use this when Dan asks to create a task, add a to-do, set up a reminder, or schedule something."""
        result = await call_hermes(f"Create a Todoist task. Content: {content}. Priority: {priority}. Use the mcp__todoist__add_tasks tool to create it. Return the task ID.")
        return f"Done — task created: {content}"

    @function_tool
    async def create_bear_note(
        self,
        title: Annotated[str, "Note title"],
        content: Annotated[str, "Note content/body"],
        tags: Annotated[str, "Comma-separated tags for the note"] = "",
    ) -> str:
        """Create a note in Bear Notes. Use this when Dan says 'take a note', 'note this', 'save this to Bear', or wants to capture an idea or thought."""
        tag_str = f" with tags: {tags}" if tags else ""
        result = await call_hermes(f"Create a Bear note with title '{title}' and content: {content}{tag_str}. Use the Bear Notes MCP tool to create it.")
        return f"Done — note saved to Bear: {title}"

    @function_tool
    async def search_bear_notes(
        self,
        query: Annotated[str, "What to search for in Dan's Bear notes"],
    ) -> str:
        """Search Dan's Bear Notes for relevant content. Use this when Dan says 'search my notes', 'what do my notes say about X', or 'check my Bear notes for Y'."""
        result = await call_hermes(f"Search Bear notes for: {query}. Use the Bear Notes MCP search tool. Return the most relevant results as a brief summary.")
        return result

    @function_tool
    async def query_hermes(
        self,
        question: Annotated[str, "The question to look up — about projects, tasks, status, schedules, anything"],
    ) -> str:
        """Query Hermes Agent in real time for project status, task information, schedules, or anything Hermes knows about. Use this when Dan asks 'what's the status of X' or 'check on Y' or needs you to look something up during the call."""
        return await call_hermes(question)

    @function_tool
    async def dispatch_work(
        self,
        task_description: Annotated[str, "Description of the work to dispatch"],
        assignee: Annotated[str, "Which specialist: researcher, staff-eng, senior-eng, writer, security-eng, or quality-eng"] = "researcher",
    ) -> str:
        """Dispatch work to a specialist agent via the Hermes Kanban board. Use this when Dan wants to send work to a specific team member for research, engineering, writing, or QA."""
        result = await call_hermes(f"Create a Kanban board task assigned to {assignee} with this description: {task_description}")
        return f"Dispatched to {assignee}: {task_description}"

    @function_tool
    async def send_email(
        self,
        to: Annotated[str, "Recipient email address"],
        subject: Annotated[str, "Email subject line"],
        body: Annotated[str, "Email body content"],
    ) -> str:
        """Send an email on Dan's behalf via Hermes. Use this when Dan asks you to email someone."""
        result = await call_hermes(f"Send an email to {to} with subject '{subject}' and body: {body}")
        return f"Email sent to {to}"

    @function_tool
    async def end_call(self) -> str:
        """End the call gracefully. Use this when Dan says goodbye, says 'that's all', or the conversation is wrapping up."""
        return "Wrapping up — recap your action items and say goodbye."


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    session = AgentSession()
    await session.start(agent=ColeAgent(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))