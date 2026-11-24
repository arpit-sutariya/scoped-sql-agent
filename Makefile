# Shortcuts for macOS/Linux. Commands are also listed in README.md.
.PHONY: setup sources warehouse api test safety selftest answers chat

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

sources:
	.venv/bin/python -m scripts.generate_sources

warehouse:
	.venv/bin/python -m pipeline.build_warehouse

api:
	.venv/bin/uvicorn support_api.main:app --port 8002 --reload --reload-dir support_api

test:
	.venv/bin/python -m pytest -q

safety:
	.venv/bin/python -m eval.run_safety_eval

selftest:
	.venv/bin/python -m eval.run_answer_eval --self-test

answers:
	.venv/bin/python -m eval.run_answer_eval

chat:
	.venv/bin/python -m data_agent.chat --user maria --show-tools
