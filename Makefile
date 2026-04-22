.PHONY: api web install

install:
	cd apps/api && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd apps/web && npm install

api:
	cd apps/api && .venv/bin/uvicorn main:app --reload --port 8000

web:
	cd apps/web && npm run dev
