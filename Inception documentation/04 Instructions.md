# Introduction

The goal is to build the "Recruiter-Facing Chief of Staff".

"The Steward" — or, more descriptively, "a Recruiter-Facing Chief of Staff" is what we are going to build.

Steward is the best fit because it carries the right connotation: someone entrusted to manage your affairs with fiduciary care, who acts on your behalf but never without your standing instruction. That is exactly the relationship you're building.

The One-Sentence Definition
The Steward is a local, self-hosted agentic harness that runs your entire recruiter-facing job search asynchronously — answering from your CV, scheduling around your calendar, drafting every reply, and escalating to you only the decisions it has not yet earned the right to make alone.

The Elevator Pitch
You walk away. The Steward watches your email, LinkedIn, and calendar. It reads every recruiter message, answers factual questions by retrieving evidence from your markdown CV, checks your availability and proposes times that don't conflict, and drafts a reply for everything else — every draft optimized to move you toward an offer.

When you return, it briefs you. Some actions it already took on its own, because you taught it those are safe. Some it queued for your judgment. A few — salary, offer stage, anything it isn't sure about — it flagged as high priority. You approve, edit, or reject in one sitting. Then you say "go," and it executes everything in a single batch: sends the emails, posts the LinkedIn replies, books the meetings.

And every decision you make is recorded. Over time it notices patterns — you always approve Kubernetes questions, you always add a buffer before calls, you never discuss salary early — and it proposes rules. You approve them. The briefing gets shorter. The agent earns autonomy one verified decision at a time.

The Three Pillars
Asynchronous by design. You don't chat with it. You brief it, and it briefs you back. The unit of interaction is a batch of decisions, not a conversation.

Earned autonomy, not assumed autonomy. It starts by asking you everything. It ends by asking you only what it hasn't learned. Every promotion up the autonomy ladder is proposed by the agent and activated only by you. Nothing self-promotes to silent execution.

Grounded in evidence. Facts come from your CV (read-only, retrieval-based, never invented). Time comes from your calendar (checked, then re-checked before booking). Judgment comes from an immutable ledger of your own past decisions. It does not guess.

What It Is Not
Not a chatbot. There is no conversation thread with the Steward. There is a queue, a briefing, and a batch.

Not a mass-apply bot. It does not spray applications. It manages the conversations that follow a human expressing interest.

Not a LinkedIn scraper. It operates through your own logged-in browser session, at human pace, with a built-in approval queue.

Not a black box. Every action traces to a draft, a decision, and a ledger entry. You can audit why anything was sent.

Not a replacement for you in the offer room. The moment a thread reaches offer stage, autonomy is revoked and everything routes to you.

The Formal Description
If you need to describe it to an engineer in one line:

A human-in-the-loop agentic harness with a graduated autonomy ladder, built on MCP servers, that manages multi-channel recruiter communications and learns a user's decision policy from an immutable decision ledger.

And if you need to describe it to a human in one line:

It's a secretary for your job search that you train by making decisions — and the more you decide, the less it asks.

The Core Insight, Compressed
The whole system rests on one idea: your decisions are the training data. Every approve, edit, and reject teaches it something. The harness is not intelligent because the LLM is smart — it's intelligent because it remembers what you did last time and refuses to ask you the same question twice without a reason.

That's the product. That's the Steward.

Inspect the conversation file C:\Users\Bernardo\Documents\Job sourcing automation\Inception documentation\03 agentic_job_sourcer_conversation.md (leave references and other text not relevant to the conversation out), determine the intent of what I want to build, use the details in that file for details on the implementation. 

Take into my consideration everything that I am asking or my requirements. 

Then build the product "The Steward". Go