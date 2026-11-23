"""Command line entry point (run with PYTHONPATH=src).

Finding work
    scout --freelancer | --file data/sample_jobs.json   find jobs
    run                                                  qualify, price, write proposals
    queue                                                proposals waiting for you
    approve <job_id> [--submit] / reject <job_id>        after you submit (or skip) a bid

Client messages
    inbox                                                draft replies to new client messages
    replies                                              reply drafts waiting for you
    send-reply <id> [--text "..."]                       send one approved reply

Delivering won jobs
    start-project <job_id> [--notes "..."]               you won the job
    build <job_id>                                       plan -> build -> QA -> package
    deliveries                                           packages ready to upload

Maintenance
    status | show <job_id> | reset-queue | requeue-rejected
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agents import Ctx, ScoutAgent
from .bid_sync import sync_bids
from .config import ROOT, load_config
from .db import DB
from .delivery import DeliveryPipeline
from .freelancer import FreelancerAPI, FreelancerAPIError, FreelancerAPISource
from .guardrails import GuardrailViolation
from .inbox import process_inbox, send_reply
from .llm import LLM, AgentRefusal, MissingAPIKey
from .orchestrator import Orchestrator
from .skills_loader import Skills
from .sources import FileSource


def freelancer_api(cfg: dict) -> FreelancerAPI:
    return FreelancerAPI(os.getenv("FREELANCER_OAUTH_TOKEN", ""), cfg["freelancer"]["environment"])


def build_ctx(config_path: str | None = None) -> Ctx:
    cfg = load_config(config_path)
    portfolio_path = Path(cfg["portfolio_file"])
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8")) if portfolio_path.exists() else []
    return Ctx(cfg=cfg, db=DB(cfg["database"]), llm=LLM(cfg), skills=Skills.load(cfg["skills_file"]), portfolio=portfolio)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freelance_agents")
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    scout = sub.add_parser("scout", help="import jobs from a source")
    src = scout.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="JSON file with a list of jobs")
    src.add_argument("--freelancer", action="store_true", help="search Freelancer.com via the official API")
    for name, help_text in (("run", "qualify, price and write proposals"), ("status", "list jobs"),
                            ("queue", "proposals and questions waiting for you"),
                            ("requeue-rejected", "re-qualify jobs the Qualifier model rejected"),
                            ("reset-queue", "re-screen and re-price queued proposals"),
                            ("inbox", "draft replies to new client messages"), ("replies", "reply drafts waiting for you"),
                            ("deliveries", "finished packages ready to upload"),
                            ("sync-bids", "record bids you placed yourself; find awards")):
        sub.add_parser(name, help=help_text)
    approve = sub.add_parser("approve", help="after you submit a queued proposal")
    approve.add_argument("job_id")
    approve.add_argument("--submit", action="store_true", help="post this one bid via the Freelancer API")
    send = sub.add_parser("send-reply", help="send one approved reply draft")
    send.add_argument("id", type=int)
    send.add_argument("--text", help="send your own text instead of the draft")
    start = sub.add_parser("start-project", help="you won the job")
    start.add_argument("job_id")
    start.add_argument("--notes", default="", help="anything agreed with the client")
    for name in ("reject", "show", "build"):
        sub.add_parser(name).add_argument("job_id")
    return p


def needs_llm(ctx: Ctx) -> bool:
    try:
        ctx.llm.check_key()
        return True
    except MissingAPIKey as e:
        print(e)
        return False


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = parser().parse_args(argv)
    ctx = build_ctx(args.config)
    orch = Orchestrator(ctx)
    db = ctx.db

    try:
        if args.cmd == "scout":
            if args.file:
                print(ScoutAgent(ctx).ingest("file", FileSource(args.file).fetch()))
            else:
                api = freelancer_api(ctx.cfg)
                source = FreelancerAPISource(api, ctx.cfg["freelancer"]["search"], ctx.cfg.get("exclude_keywords", []))
                records = source.fetch()
                print(f"Freelancer ({api.environment}): {source.stats}")
                print(f"{len(records)} unique projects after filters")
                print(ScoutAgent(ctx).ingest("freelancer_api", records))

        elif args.cmd == "run":
            if not needs_llm(ctx):
                return 1
            print(orch.tick())

        elif args.cmd == "status":
            rows = db.all_jobs()
            print(f"{'JOB':28} {'STATE':18} {'COST':>8}  TITLE")
            for r in rows:
                print(f"{r['job_id']:28} {r['state']:18} ${r['cost_usd']:>7.4f}  {json.loads(r['data'])['title'][:50]}")
            print(f"\nTotal AI cost: ${sum(r['cost_usd'] for r in rows):.4f}")

        elif args.cmd == "queue":
            for item in db.pending_approvals("proposal"):
                p = json.loads(item["payload"])
                job = json.loads(db.get_job(item["job_id"])["data"])
                print(f"=== {item['job_id']} | {p['bid_price']['amount']:g} {p['bid_price']['currency']} | "
                      f"{p['timeline_days']} days\n{job['url']}\n")
                print(p["proposal_text"], "\n")
            for item in db.pending_approvals("questions"):
                print(f"=== QUESTIONS for {item['job_id']} (ask the client) ===")
                for q in json.loads(item["payload"])["questions"]:
                    print(f"- {q}")
            print("After submitting a proposal on the platform yourself, run: approve <job_id>")

        elif args.cmd == "reset-queue":
            queued = [r["job_id"] for r in db.jobs_in_state("awaiting_approval")]
            fl_ids = [int(j.split(":", 1)[1]) for j in queued if j.startswith("freelancer:")]
            refreshed = {}
            if fl_ids and os.getenv("FREELANCER_OAUTH_TOKEN"):
                refreshed = {j["job_id"]: j for j in freelancer_api(ctx.cfg).projects_by_id(fl_ids)}
            ids = db.reset_queued(refreshed)
            print(f"reset {len(ids)} queued jobs ({len(refreshed)} refreshed from Freelancer); run `run` to re-price them")

        elif args.cmd == "requeue-rejected":
            print(f"requeued {db.requeue_model_rejections()} jobs; run `run` to re-qualify them")

        elif args.cmd == "approve":
            api = freelancer_api(ctx.cfg) if args.submit and not ctx.cfg["dry_run"] else None
            print(f"{args.job_id} -> submitted ({orch.approve(args.job_id, submit=args.submit, api=api)})")

        elif args.cmd == "reject":
            orch.reject(args.job_id)
            print(f"{args.job_id} -> rejected")

        elif args.cmd == "inbox":
            if not needs_llm(ctx):
                return 1
            print(process_inbox(ctx, freelancer_api(ctx.cfg)))

        elif args.cmd == "replies":
            for item in db.pending_approvals("reply"):
                p = json.loads(item["payload"])
                flag = "OK to send" if p["send_allowed"] else "BLOCKED: write your own with --text"
                esc = f" | ESCALATED: {p['escalation_reason']}" if p.get("escalate") else ""
                print(f"=== #{item['id']} {item['job_id']} | {p['intent_detected']} | {flag}{esc}")
                print(f"CLIENT: {p['client_message']}\nDRAFT:  {p['reply_text']}\n")
            print("Send one with: send-reply <id>   (or send-reply <id> --text \"your words\")")

        elif args.cmd == "send-reply":
            api = None if ctx.cfg["dry_run"] else freelancer_api(ctx.cfg)
            print(send_reply(ctx, api, args.id, args.text))

        elif args.cmd == "start-project":
            DeliveryPipeline(ctx).start(args.job_id, args.notes)
            print(f"{args.job_id} -> planning. Next: build {args.job_id}")

        elif args.cmd == "build":
            if not needs_llm(ctx):
                return 1
            print(DeliveryPipeline(ctx).run(args.job_id))

        elif args.cmd == "sync-bids":
            print(sync_bids(ctx, freelancer_api(ctx.cfg)))

        elif args.cmd == "deliveries":
            for item in db.pending_approvals("delivery"):
                p = json.loads(item["payload"])
                print(f"=== #{item['id']} {item['job_id']}\nPackage: {p['zip']}")
                for warning in p.get("check_before_sending") or []:
                    print(f"CHECK: {warning}")
                print(f"\n{p['handoff_notes_md']}\n")
            print("Upload the package to the client on Freelancer yourself.")

        elif args.cmd == "show":
            row = db.get_job(args.job_id)
            if row is None:
                print("not found")
                return 1
            print(f"state: {row['state']}  cost: ${row['cost_usd']:.4f}")
            for o in db.outputs(args.job_id):
                print(f"\n--- {o['agent']} ({o['created_at']}) ---")
                print(json.dumps(json.loads(o["output"]), indent=2))

    except (FreelancerAPIError, GuardrailViolation, AgentRefusal, ValueError) as e:
        print(f"{args.cmd}: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
