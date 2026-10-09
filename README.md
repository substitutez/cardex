# CarDex — Automotive SpotterDex & Vehicle Intelligence Agent

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Cloud%20Run-blue?logo=google-cloud&style=for-the-badge)](https://cardex-frontend-861697384142.us-east1.run.app)
[![Agent Runtime](https://img.shields.io/badge/Google%20Agent%20Engine-Deployed-brightgreen?logo=google&style=for-the-badge)](https://cardex-frontend-861697384142.us-east1.run.app)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

**CarDex** is an agentic AI assistant built on the Google Agent Development Kit (ADK) and Google Cloud Agent Platform. Designed as a real-world "Pokédex for cars", CarDex enables automotive spotters and enthusiasts to identify vehicles from photos, track sightings in a personal garage, score points based on rarity tiers, query comprehensive production specifications, and view community leaderboards.

🏎️ **Try the Live App**: [https://cardex-frontend-861697384142.us-east1.run.app](https://cardex-frontend-861697384142.us-east1.run.app)

---

## 🏎️ What CarDex Does

CarDex acts as an interactive automotive companion with real backend integrations:

1. **Multimodal Car Identification**:
   - Analyzes user-submitted car photos or image URLs using Gemini Vision in the global region.
   - Identifies make, model, trim, factory colorway, and special edition status.
   - Uploads spotted car photos directly to Google Cloud Storage in-memory and returns public HTTPS image links.
   - Automatically cross-references the catalog and records new sightings.

2. **Pokédex-Style Spotting & Personal Garage**:
   - **Strict Live Device Camera Verification**: Enforces hardware camera capture via W3C HTML Media Capture (`capture="environment"`) on mobile and an in-app Cockpit Camera HUD reticle on desktop to guarantee authentic real-world sightings (album and file uploads disabled).
   - Classifies vehicles into 6 rarity tiers: **Mythic 1-of-1**, **Legendary**, **Epic**, **Rare**, **Uncommon**, and **Common**.
   - Awards spotter points and tracks sighting streaks.
   - Manages personal garages in Google Cloud Firestore, recording spot timestamps, user collections, and global Shazam-style spot counts.

3. **Multi-Source Vehicle Specifications & Verification**:
   - **Official Federal NHTSA vPIC Registry**: Decodes 17-digit VIN numbers and looks up official manufacturer model registrations (100% free, uncapped federal data).
   - **Comprehensive Vehicle Specs Database**: Queries an indexed SQLite database covering 164 makes and 2,649 models for horsepower, top speed, 0–100 km/h acceleration, torque, transmission, and engine configuration.
   - **CarAPI Live Trims & OEM Paints**: Queries live trim pricing and factory exterior paint names with exact RGB color codes.
   - **Wikimedia Commons Provenance**: Fetches verified vehicle images and historical background dossiers.
   - **Google Search Grounding & Deep Research**: Uses Google Search and a multi-agent research pipeline for rare bespoke 1-of-1s, auction records, and unlisted builds.

4. **Community Leaderboards & Dispute Reviews**:
   - Queries global spotter leaderboards ranked by total points and unique finds in Firestore.
   - Accepts spot correction submissions and trim disputes into Firestore for human review.

5. **User Authentication & Spotter Persistence**:
   - Secure sign-in and sign-up with PBKDF2-HMAC-SHA256 password hashing and session tokens.
   - Saves vehicle submissions, photo URLs, and rarity points permanently to **Google Cloud Firestore**.
   - Live synchronization of spotter prestige ranks (*Rookie Spotter* to *Apex Legend*) and real-time points updates.
   - Dedicated "My Spotter Submissions & Garage" modal with full photo history and sighting metadata.
   - 1-click demo login option (`@Dan_the_spotter` · 108,000 PTS · 12 exotic supercars).

6. **Cross-Session Long-Term Memory**:
   - Integrated with **Vertex AI Memory Bank** to retain spotter preferences, favorite marques, dream cars, unit preferences (km/h vs. mph), and dietary/environmental sensitivities across sessions.

7. **Agent Platform Code Sandbox Execution**:
   - Executes Python code inside a secure Agent Engine Sandbox (`AgentEngineSandboxCodeExecutor`) to compute rarity multipliers, unit conversions, and scoring algorithms.

8. **A2UI Rich Card Rendering & Mobile-Optimized Cockpit Frontend**:
   - Emits structured A2UI (v0.8 Basic Catalog) components (Cards, Columns, Rows, Text, Images) for clean visual card displays.
   - Includes a standalone FastAPI proxy and cockpit-themed chat web interface communicating over the Agent-to-Agent (A2A) protocol.
   - **Interactive Cockpit Sidebar**: Categorized quick prompts for Garage, Verification, and Field Spotting.
   - **Mobile & Touch Ready**: Dynamic viewport height (`100dvh`), Apple notch & home indicator safe area insets, touch swipe drawer gestures, and virtual keyboard auto-scroll.
   - **Seamless Design**: Thematic styling with custom invisible scrollbars preserving the deep cockpit aesthetic.

---

## 🛠️ Architecture & Wired Tools

Based on `app/agent.py` and `agents-cli-manifest.yaml`, the following tools and services are wired into the agent:

### Function Tools (`app/car_tools.py`)

| Tool Name | Source / Provider | Description |
| :--- | :--- | :--- |
| `identify_and_spot_car` | Gemini Vision + GCS | Analyzes car images, uploads to GCS, determines make/model/trim/rarity, and logs the spot. |
| `lookup_car_in_cardex` | Firestore | Queries the CarDex vehicle database for specs, rarity tier, base points, and global spot count. |
| `list_cardex_cars` | Firestore | Lists cars in the catalog filtered by manufacturer or rarity tier. |
| `record_car_spot` | Firestore | Logs a sighting to the user's garage, increments spotter points, and updates global count. |
| `view_my_garage` | Firestore | Retrieves the user's collection of spotted vehicles and total score. |
| `view_leaderboard` | Firestore | Fetches the global spotter rankings ordered by total score. |
| `submit_car_review` | Firestore | Logs a dispute or correction request for a misclassified or unlisted build. |
| `decode_vin_specifications` | NHTSA vPIC API | Decodes 17-digit VINs into federal safety and powertrain specifications. |
| `lookup_nhtsa_models_by_year` | NHTSA vPIC API | Queries federal database for all models registered by a make in a specific model year. |
| `search_vehicle_specs_database` | SQLite (`vehicles.sqlite`) | Queries technical specs (HP, top speed, 0-100, torque) across 164 makes & 2,649 models. |
| `fetch_carapi_live_specs` | CarAPI | Fetches live MSRP pricing, trim options, and OEM paint colors with RGB values. |
| `fetch_car_image_and_provenance` | Wikimedia API | Retrieves verified vehicle photos and historical provenance summaries. |
| `google_search` | Google Search Tool | Real-time web search grounding for live facts and auction records. |
| `AgentTool(plan_generator)` | ADK Sub-Agent | Research planner and pipeline for in-depth vehicle investigation. |

### Platform Services & Integrations

- **Agent Framework**: Google ADK (Agent Development Kit) `LlmAgent` with `BuiltInPlanner`.
- **Memory Service**: `VertexAiMemoryBankService` with `PreloadMemoryTool` and post-turn memory extraction.
- **Code Execution**: `AgentEngineSandboxCodeExecutor` running on Google Cloud Agent Engine.
- **Rich Display**: `A2uiSchemaManager` with `BasicCatalog` (version 0.8) attached via `after_model_callback`.
- **Database**: Google Cloud Firestore (Collections: `cars`, `garages`, `reviews`).
- **Object Storage**: Google Cloud Storage (Bucket for spotter photo uploads).
- **Communication Protocol**: A2A (Agent-to-Agent) protocol over Agent Engine HTTP passthrough.

---

## 📁 Repository Structure

```
cardex/
├── app/
│   ├── agent.py               # Root LlmAgent definition, instructions, tools & callbacks
│   ├── car_tools.py           # 12 function tool implementations (Vision, NHTSA, CarAPI, etc.)
│   ├── firestore_db.py        # Firestore client and database operations
│   ├── vehicle_db.py          # SQLite engine for 2,649 vehicle models with GCS auto-fetch
│   ├── a2ui_utils.py          # A2UI model callback for card rendering
│   ├── config.py              # Agent model and runtime configuration
│   ├── fast_api_app.py        # Local FastAPI agent server
│   └── app_utils/             # A2A adapters, reasoning engine connectors, and services
├── frontend/
│   ├── main.py                # FastAPI proxy translating chat requests to A2A protocol
│   ├── auth_db.py             # User authentication, PBKDF2 hashing & Firestore spotter persistence
│   ├── requirements.txt       # Frontend proxy dependencies
│   ├── Procfile               # Cloud Run deployment entrypoint
│   └── static/
│       └── index.html         # Cockpit-themed chat UI with auth modal, garage viewer & A2UI
├── tests/
│   ├── test_auth.py           # User authentication and Firestore persistence tests
│   └── unit/                  # Unit test suite verifying agent structure, tools, and DBs
├── agents-cli-manifest.yaml   # Deployment manifest for agents-cli
├── pyproject.toml             # Python dependencies and build configuration
└── README.md                  # Project documentation
```

---

## 🚀 How to Run and Use

### 1. Prerequisites

- Python 3.11+
- `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Google Cloud SDK (`gcloud`) authenticated with a GCP project:
  ```bash
  gcloud auth login
  gcloud auth application-default login
  gcloud config set project <YOUR_GCP_PROJECT_ID>
  ```

### 2. Environment Configuration

Create a `.env` file in the root `cardex/` directory:

```bash
# Vertex AI / GCP Configuration
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=<YOUR_GCP_PROJECT_ID>
GOOGLE_CLOUD_LOCATION=us-east1

# Optional CarAPI Key (falls back to public endpoints if omitted)
CARAPI_KEY=your_key_here
CARAPI_SECRET=your_secret_here
```

### 3. Install Dependencies

Install the project dependencies using `uv`:

```bash
uv sync
```

### 4. Run Unit Tests

Execute the automated test suite to verify agent configuration and tools:

```bash
uv run pytest tests/unit
```

### 5. Launch the Agent Playground (Local ADK)

Start the local agent development playground:

```bash
uv run adk web . --port 8080 --reload_agents
```

Navigate to ``http://localhost:8080`` in your web browser to interact directly with the agent, test tool calls, and inspect execution traces.

### 6. Run the Cockpit Chat Web Frontend Locally

The standalone frontend proxy provides the branded CarDex user interface and talks to the agent over A2A:

```bash
cd frontend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set the deployed reasoning engine resource (or target runtime)
export AGENT_ENGINE_RESOURCE_NAME="projects/<PROJECT_NUMBER>/locations/us-east1/reasoningEngines/<ENGINE_ID>"
export AGENT_DIRECTORY="app"

python main.py
```

Open ``http://localhost:8080`` to access the chat UI with quick-action example prompts:
- *"Look up specs and rarity for 2024 Porsche 911 GT3 RS"*
- *"Show the global spotter leaderboard and rarest spots"*
- *"Check my garage collection and spotter score"*

---

## ☁️ Deployment

### Deploying the Agent to Agent Platform

Deploy the agent runtime container using `agents-cli`:

```bash
agents-cli deploy \
  --project=<YOUR_GCP_PROJECT_ID> \
  --region=us-east1 \
  --service-account=<SERVICE_ACCOUNT_EMAIL>
```

### Deploying the Frontend to Google Cloud Run

Deploy the web frontend to Cloud Run:

```bash
cd frontend

gcloud run deploy cardex-frontend \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --set-env-vars="AGENT_ENGINE_RESOURCE_NAME=projects/<PROJECT_NUMBER>/locations/us-east1/reasoningEngines/<ENGINE_ID>,AGENT_DIRECTORY=app"
```

Ensure the Cloud Run service account has the **`roles/aiplatform.user`** IAM role so it can query the deployed agent runtime over the A2A protocol.

---

## 🔒 Security & Best Practices

- **Zero Hardcoded Secrets**: Sensitive API credentials and authentication tokens are loaded from environment variables and Application Default Credentials (ADC).
- **In-Memory Image Processing**: Uploaded vehicle spot images are streamed directly to Cloud Storage in memory without writing temporary files to local disks.
- **Sandboxed Execution**: Mathematical computations and rarity multipliers run inside an isolated Google Cloud Agent Engine container sandbox.
