# Gemini Executive Demo Playbooks — Interactive 3-Book Storybook Rack

An interactive, three-volume executive workshop rack built for hands-on sessions across **Gemini Enterprise App**, **Gemini Agent**, and **Google Antigravity**.

## Volumes Included

1. **Book 01 · Gemini Enterprise App (`6 Chapters`)**
   - Governed browser AI grounded across live Web Search, Microsoft 365 (`Outlook`, `SharePoint`, `OneDrive`), and Google Workspace.
   - Hands-on chapters for **Search & Canvas**, **Personalizations & Deep Research**, **Skills & Projects**, **Gemini Notebook Enterprise** (with live voice *Join*), and **ADK & 3P Agent Invocation** (`@Ask HR`, `@3D Rendering`, `@Workday`, `@SAP`, `@ServiceNow`).
2. **Book 02 · Gemini Agent (`6 Chapters`)**
   - **The Core Shift & Four Eras of AI Work**: Moving from reactive chat boxes to a persistent, 24/7 cloud coworker with its own isolated computer and workspace.
   - **Enterprise Controls & Guardrails**: CISO & CFO controls (Attested Identity, RBAC & ethical walls, Dual Audit Trail, Agent Sandbox & Gateway, per-project spend caps, and multi-model routing).
   - **Three Knowledge-Worker Playbooks**:
     - **Execs — Chief of Staff**: Upload [`assets/Digital_Chief_of_Staff_Agent.docx`](assets/Digital_Chief_of_Staff_Agent.docx) to compile and invoke a reusable M365 Chief of Staff skill.
     - **Finance — Earnings Analysis**: Upload [`assets/Alphabet_Q3_2026_Form_10-Q.pdf`](assets/Alphabet_Q3_2026_Form_10-Q.pdf), analyze segment performance, and export live-formula Excel (`.xlsx`) models.
     - **Legal — CLM Skills**: Run a 4-skill Contract Lifecycle Management suite across Playbook guardrails, BigQuery precedents, cross-clause Tariff/FFP checks, and Word OOXML Track Changes (`.docx`).
3. **Book 03 · Google Antigravity (`7 Chapters`)**
   - Platform overview (Command Center UI, Dynamic Subagents, Rich Artifacts, Scheduled Tasks, and Plugin Marketplace).
   - Guided hands-on build tracks for **Something from Scratch** and **Evolving V1 → V2** across **Vibe Coding** (`/grill-me`, `/teamwork`, `/browser`, `/goal`) and **Spec-Driven Development** (**Spec to Ship** 8-phase plugin).

## Run Locally

```bash
# Optional: copy .env.example to .env if you want to enable the password gate locally
python3 serve.py
```

Then open `http://localhost:8847` in your browser (and `http://localhost:8847/feedback` for the collaborative comment & inline-edit report).

- Press **`E`** (or click **✏️ EDIT**) to edit any cover or page inline.
- Press **`N`** for facilitator notes or **`P`** for the synchronized presenter stopwatch window.

## Deploy to Google Cloud Run

```bash
gcloud run deploy executive-storybooks \
  --source=. \
  --region=us-central1 \
  --allow-unauthenticated \
  --set-env-vars="STORYBOOK_PASSWORD=your-password,GCS_BUCKET=your-gcs-bucket"
```
