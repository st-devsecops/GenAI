# BharatVoice AI

## Multilingual Voice-First Ordering and Billing Assistant for Indian Kirana Stores

**Project status:** Architecture and product design\
**Initial release:** MVP\
**Initial languages:** Kannada (`kn-IN`), Telugu (`te-IN`), Tamil
(`ta-IN`), Malayalam (`ml-IN`)

BharatVoice AI is a proposed voice-first commerce platform that lets
customers place grocery orders by calling a shop phone number. The
customer selects a language, speaks naturally, reviews the recognized
order, confirms it, and receives a bill in the same language. The AI
handles routine calls autonomously and transfers to a shopkeeper when it
cannot safely complete the request.

> **Important:** This README is the project blueprint and initial
> development guide. It does not claim that the application,
> integrations, APIs, or deployment are already implemented. Technology
> providers and language coverage must be validated before production
> use.

------------------------------------------------------------------------

## 1. Product vision

Build an accessible, multilingual voice assistant for small retailers
and Kirana stores, starting with the four major South Indian languages.

### Customer experience

1.  Customer calls the shop's published phone number.
2.  The AI answers and offers a language menu:
    -   Press 1 --- Kannada
    -   Press 2 --- Telugu
    -   Press 3 --- Tamil
    -   Press 4 --- Malayalam
3.  The AI greets the customer in the selected language and asks what
    they would like to order.
4.  Customer speaks their order naturally, including quantities and
    units.
5.  Speech recognition converts the audio into text.
6.  The AI extracts product names, quantities and units.
7.  The catalogue and inventory services match the request to products
    sold by that specific shop.
8.  The system asks clarification questions if a product, quantity, unit
    or substitution is uncertain.
9.  The AI reads back the complete order and prices in the selected
    language.
10. Customer can add, remove or change items.
11. The customer explicitly confirms the order.
12. The deterministic billing service calculates the bill.
13. The system generates a localized invoice and sends it through an
    approved WhatsApp Business or SMS integration.
14. The shopkeeper receives the order notification and can review it in
    the dashboard.

### Core product principles

-   Voice-first: customers should not need to use an app or type a form.
-   Language continuity: retain the customer's selected language
    throughout the call.
-   Safe ordering: never silently guess an item, quantity, price or
    customer confirmation.
-   Deterministic billing: prices, taxes, discounts and totals come from
    validated business data and code, not an LLM.
-   Human fallback: transfer to a shopkeeper when needed or whenever the
    customer asks.
-   Multi-shop design: each shop has its own catalogue, prices, stock,
    customers and orders.
-   Extensible platform: the voice and language layer should be reusable
    for future civic-service and education use cases.

------------------------------------------------------------------------

## 2. Initial scope

### In scope for the MVP

-   Incoming phone calls
-   IVR language selection for four South Indian languages
-   Speech-to-text and text-to-speech integration
-   Multilingual conversation orchestration
-   Product and quantity extraction
-   Regional product aliases and common spoken names
-   Shop-specific product catalogue
-   Inventory availability checks
-   Draft order creation and modification
-   Explicit customer confirmation
-   Accurate bill calculation
-   Localized PDF invoice
-   WhatsApp/SMS invoice delivery, subject to provider approval
-   Shopkeeper dashboard
-   Human handoff
-   Audit trail, monitoring and basic operational metrics

### Out of scope for the first MVP

-   Online payment collection
-   Delivery partner assignment and tracking
-   Complex ERP/POS integrations
-   Autonomous purchasing or stock replenishment
-   Civic services and education workflows
-   All-India language coverage
-   Separate microservice deployment for every backend module

These can be considered after the core ordering workflow is reliable.

------------------------------------------------------------------------

## 3. High-level architecture

``` mermaid
flowchart TB
    CUSTOMER["Customer"]
    PSTN["Phone / PSTN"]
    TELE["Telephony Provider"]
    IVR["IVR: Language Selection"]
    AUDIO["Real-time Audio Gateway"]

    subgraph AI["Multilingual AI Platform"]
      ASR["Speech-to-Text"]
      LANG["Language Context"]
      ORCH["Conversation Orchestrator"]
      EXTRACT["Intent and Product Extraction"]
      MATCH["Product Matching"]
      TTS["Text-to-Speech"]
    end

    subgraph COMMERCE["Commerce Backend"]
      CATALOG["Product Catalogue"]
      STOCK["Inventory"]
      ORDER["Order Management"]
      BILL["Billing Engine"]
      INVOICE["Invoice Generator"]
    end

    subgraph DATA["Data Layer"]
      PG[("PostgreSQL")]
      REDIS[("Redis")]
    end

    DASH["Shopkeeper Dashboard"]
    HANDOFF["Human Handoff"]
    MSG["WhatsApp / SMS"]

    CUSTOMER --> PSTN --> TELE --> IVR --> AUDIO
    AUDIO --> ASR --> LANG --> ORCH
    ORCH --> EXTRACT --> MATCH --> CATALOG
    CATALOG --> STOCK
    ORCH <--> REDIS
    ORCH --> ORDER --> BILL --> INVOICE
    INVOICE --> PG
    CATALOG --> PG
    STOCK --> PG
    ORDER --> PG
    INVOICE --> MSG
    ORCH --> TTS --> AUDIO
    ORCH --> HANDOFF --> DASH
    ORDER --> DASH
```

### Architectural responsibility boundaries

  -----------------------------------------------------------------------
  Component                           Responsibility
  ----------------------------------- -----------------------------------
  Telephony provider                  Receive calls, IVR, audio
                                      transport, call events and transfer

  Audio gateway                       Connect the live call to speech
                                      services

  ASR                                 Convert customer speech into text

  Language context                    Preserve the selected language and
                                      handle code-switching

  Conversation orchestrator           Control the order conversation and
                                      workflow state

  LLM extraction                      Identify intent, product mentions,
                                      quantities and units

  Product matcher                     Match extracted phrases against the
                                      shop catalogue and aliases

  Catalogue service                   Return authoritative product names
                                      and prices

  Inventory service                   Validate stock and availability

  Order service                       Maintain draft, confirmed and
                                      cancelled orders

  Billing engine                      Calculate line totals, taxes,
                                      discounts and grand total

  Invoice service                     Generate invoice data and localized
                                      PDF

  Notification service                Deliver invoice and order
                                      notifications

  Dashboard                           Allow shopkeepers to manage
                                      products, orders and escalations
  -----------------------------------------------------------------------

**Critical separation:** the LLM interprets customer language; business
services validate the request; deterministic code calculates the bill.
The LLM must not directly update inventory, commit orders or invent
prices.

------------------------------------------------------------------------

## 4. Supported languages

  Language    Internal code   Locale to validate
  ----------- --------------- --------------------
  Kannada     `kn`            `kn-IN`
  Telugu      `te`            `te-IN`
  Tamil       `ta`            `ta-IN`
  Malayalam   `ml`            `ml-IN`

Each language requires evaluation using real speakers, regional accents,
different speaking speeds, background shop noise and common
code-switching with English.

Language support includes:

-   Speech recognition
-   Product and quantity extraction
-   Regional product aliases
-   Clarification questions
-   Order read-back
-   Error and fallback messages
-   Text-to-speech
-   Invoice labels and product descriptions

Do not assume that a provider supports all four languages equally well.
Benchmark each language independently before selecting the production
ASR and TTS providers.

------------------------------------------------------------------------

## 5. Technology choices

The following are proposed starting choices, not finalized vendor
commitments.

  -----------------------------------------------------------------------
  Area                    Proposed technology     Notes
  ----------------------- ----------------------- -----------------------
  Backend                 Python, FastAPI         REST APIs and webhooks

  AI workflow             LangGraph               Explicit conversation
                                                  state and controlled
                                                  transitions

  LLM                     Gemini or another       Structured extraction
                          validated multilingual  and intent
                          model                   understanding

  ASR                     Bhashini or another     Benchmark all four
                          validated provider      languages

  TTS                     Bhashini or another     Evaluate naturalness
                          validated provider      and latency

  Telephony               Exotel or Knowlarity    Validate Indian number
                                                  provisioning, streaming
                                                  and transfer

  Database                PostgreSQL              Relational business
                                                  data

  Cache/session state     Redis                   Short-lived state and
                                                  coordination

  Frontend                React + TypeScript      Shopkeeper dashboard

  Invoice                 Python PDF library      Localized PDF output

  Notifications           WhatsApp Business       Requires approved
                          Platform / SMS provider business setup

  Containers              Docker and Docker       Local development
                          Compose                 

  Later deployment        Kubernetes              Only after the
                                                  application is stable
  -----------------------------------------------------------------------

### Provider selection checklist

Before finalizing a provider, validate:

-   Support for Indian inbound calling and required number types
-   IVR and DTMF support
-   Bidirectional audio streaming and supported codecs
-   Webhook security and retry behavior
-   Call transfer and fallback behavior
-   ASR language and dialect coverage
-   TTS voice quality and language coverage
-   Latency, rate limits and service availability
-   Data retention, privacy, consent and contractual terms
-   Pricing per call, minute, API request and message

------------------------------------------------------------------------

## 6. Proposed repository structure

The initial implementation should be a **modular monolith**. Keep clear
module boundaries, but do not create a separate deployed service for
every module before there is a scaling or operational need.

``` text
bharatvoice-ai/
├── README.md
├── .gitignore
├── .env.example
├── docker-compose.yml
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/
│   │   │   └── routes/
│   │   │       ├── calls.py
│   │   │       ├── orders.py
│   │   │       ├── products.py
│   │   │       ├── invoices.py
│   │   │       └── webhooks.py
│   │   ├── voice/
│   │   │   ├── telephony.py
│   │   │   ├── ivr.py
│   │   │   ├── audio_stream.py
│   │   │   └── handoff.py
│   │   ├── ai/
│   │   │   ├── orchestrator.py
│   │   │   ├── asr.py
│   │   │   ├── tts.py
│   │   │   ├── language.py
│   │   │   ├── extraction.py
│   │   │   └── prompts/
│   │   ├── commerce/
│   │   │   ├── catalogue.py
│   │   │   ├── product_matcher.py
│   │   │   ├── inventory.py
│   │   │   ├── order_manager.py
│   │   │   └── billing.py
│   │   ├── invoice/
│   │   │   ├── generator.py
│   │   │   └── localization.py
│   │   ├── notifications/
│   │   │   ├── whatsapp.py
│   │   │   └── sms.py
│   │   ├── database/
│   │   │   ├── models/
│   │   │   ├── migrations/
│   │   │   └── session.py
│   │   └── monitoring/
│   │       ├── metrics.py
│   │       └── logging.py
│   └── tests/
│
├── frontend/
│   ├── Dockerfile
│   └── src/
│       ├── pages/
│       ├── components/
│       ├── services/
│       └── dashboard/
│
├── database/
│   ├── schema.sql
│   └── seed/
│
├── infrastructure/
│   ├── docker/
│   ├── kubernetes/
│   └── terraform/
│
└── docs/
    ├── architecture.md
    ├── database-design.md
    ├── api-contracts.md
    ├── language-support.md
    └── threat-model.md
```

This is the target structure. Files should be added as the corresponding
implementation phase begins.

------------------------------------------------------------------------

## 7. Database design

Use PostgreSQL for transactional business data.

### Core entities

  -----------------------------------------------------------------------
  Table                               Purpose
  ----------------------------------- -----------------------------------
  `shops`                             Shop profile, phone number, address
                                      and default language

  `shop_users`                        Shopkeeper and employee accounts

  `customers`                         Customer contact and preferred
                                      language

  `supported_languages`               Supported language and locale
                                      configuration

  `products`                          Shop-specific product catalogue

  `product_aliases`                   Product names and spoken aliases in
                                      regional languages

  `product_prices`                    Effective-dated selling prices

  `inventory`                         Current stock by shop and product

  `orders`                            Order header, customer, language
                                      and status

  `order_items`                       Product, quantity, unit price and
                                      line total

  `calls`                             Telephony call identifiers and call
                                      status

  `conversation_sessions`             Conversation state and selected
                                      language

  `conversation_turns`                Conversation event metadata,
                                      subject to retention policy

  `invoices`                          Invoice number, totals and document
                                      reference

  `payments`                          Future payment status and
                                      references

  `audit_events`                      Important business and security
                                      events
  -----------------------------------------------------------------------

### Important data rules

-   Use UUID primary keys where appropriate.
-   Store money as `NUMERIC`, never floating-point.
-   Store timestamps consistently in UTC and display them in the shop's
    configured timezone.
-   Preserve the unit of measure and quantity precision.
-   Store the price snapshot on each confirmed order item so future
    catalogue price changes do not rewrite historical bills.
-   Enforce unique invoice numbers per shop.
-   Enforce tenant isolation in every shop-owned query.
-   Use migrations to evolve the schema.

### Multi-tenant relationship

A single platform serves many shops. Each shop has its own products,
aliases, prices, inventory and orders. A customer may be associated with
more than one shop, so customer identity and shop relationship should be
modeled deliberately rather than assuming a globally unique customer per
shop.

------------------------------------------------------------------------

## 8. Order lifecycle and state machine

``` mermaid
stateDiagram-v2
    [*] --> IncomingCall
    IncomingCall --> LanguageSelection
    LanguageSelection --> Ordering
    Ordering --> ProductValidation
    ProductValidation --> Ordering: Clarification needed
    ProductValidation --> OrderReview: Valid draft
    OrderReview --> Ordering: Customer edits
    OrderReview --> Confirmed: Customer explicitly confirms
    Confirmed --> Billing
    Billing --> InvoiceGeneration
    InvoiceGeneration --> Completed
    Ordering --> HumanHandoff: Escalation
    ProductValidation --> HumanHandoff: Repeated failure
    Billing --> BillingFailed: Calculation or service failure
    BillingFailed --> HumanHandoff
    HumanHandoff --> Completed: Human resolves
    Ordering --> Cancelled: Customer cancels
    Completed --> [*]
    Cancelled --> [*]
```

### Order safety rules

1.  A draft is not a confirmed order.
2.  The customer must explicitly confirm the complete read-back.
3.  A changed order must be reviewed and confirmed again.
4.  The backend must validate product IDs, prices, stock and quantities.
5.  Use idempotency keys to prevent duplicate order creation on retries.
6.  If a call drops, persist the draft state and apply an appropriate
    expiry/resume policy.
7.  If invoice delivery fails, retain the valid order and retry delivery
    separately.
8.  Do not claim that a payment has been made unless a payment provider
    confirms it.

------------------------------------------------------------------------

## 9. AI workflow

Use LangGraph or an equivalent explicit workflow orchestrator.

### Processing sequence

1.  Receive audio from the telephony gateway.
2.  Convert speech to text using the selected ASR provider.
3.  Preserve the customer's selected language.
4.  Detect the intent: add item, remove item, change quantity, review,
    confirm, cancel or request a human.
5.  Extract product mentions, quantities and units into a strict schema.
6.  Match product phrases against the current shop catalogue and
    regional aliases.
7.  Validate stock and retrieve authoritative prices.
8.  Ask a localized clarification question if matching or quantity
    resolution is ambiguous.
9.  Read the current order back to the customer.
10. Wait for explicit confirmation.
11. Submit the confirmed order through the order service.
12. Generate the bill and localized invoice.
13. Speak the result and send the invoice.

### Example structured extraction

Customer says:

> I need 2 kg rice, 1 litre oil, 1 kg sugar and half kg toor dal.

Illustrative extraction:

``` json
{
  "language": "kn",
  "intent": "create_order",
  "items": [
    {"product_query": "rice", "quantity": 2, "unit": "kg"},
    {"product_query": "cooking_oil", "quantity": 1, "unit": "litre"},
    {"product_query": "sugar", "quantity": 1, "unit": "kg"},
    {"product_query": "toor_dal", "quantity": 0.5, "unit": "kg"}
  ],
  "needs_clarification": false
}
```

This is an example schema, not a claim that the model has achieved a
particular accuracy. The backend must validate all fields before
creating or changing an order.

### Product matching

Use a layered approach:

1.  Exact SKU or canonical product ID
2.  Exact normalized alias in the selected language
3.  Fuzzy matching against shop-specific aliases
4.  Semantic matching as a candidate-generation aid
5.  Customer clarification if multiple products remain plausible

Never use semantic similarity alone to choose between materially
different products or brands.

------------------------------------------------------------------------

## 10. Human handoff

The AI should handle routine orders without shopkeeper intervention. It
must escalate safely when needed.

  -----------------------------------------------------------------------
  Scenario                            Expected behavior
  ----------------------------------- -----------------------------------
  Product not understood              Ask the customer to repeat or
                                      clarify

  Quantity unclear                    Ask for the quantity and unit

  Multiple possible products          Read out choices and ask

  Product unavailable                 Offer only verified alternatives

  Repeated speech recognition         Transfer to shopkeeper
  failures                            

  Customer asks for a human           Transfer immediately

  Billing or order service            Do not falsely confirm; escalate or
  unavailable                         arrange callback

  Shopkeeper unavailable              Save the request and follow the
                                      configured callback process
  -----------------------------------------------------------------------

The telephony provider must support call transfer or an equivalent
warm-transfer workflow. If transfer is not possible, provide a clear
fallback such as a callback request.

------------------------------------------------------------------------

## 11. API outline

Proposed API version: `/api/v1`

  ------------------------------------------------------------------------------
  Method                  Endpoint                       Purpose
  ----------------------- ------------------------------ -----------------------
  POST                    `/calls/incoming`              Receive authenticated
                                                         telephony webhook

  POST                    `/calls/{call_id}/language`    Save language selection

  WS                      `/voice/stream/{call_id}`      Real-time audio
                                                         transport, if supported
                                                         by provider

  POST                    `/ai/extract-order`            Extract structured
                                                         order details

  GET                     `/products`                    Search the
                                                         authenticated shop's
                                                         catalogue

  POST                    `/orders`                      Create draft order

  PATCH                   `/orders/{order_id}`           Modify draft order

  POST                    `/orders/{order_id}/confirm`   Confirm draft order

  POST                    `/orders/{order_id}/bill`      Generate bill for
                                                         confirmed order

  GET                     `/invoices/{invoice_id}`       Retrieve invoice

  POST                    `/handoff`                     Request human
                                                         escalation

  GET                     `/dashboard/orders`            List authorized shop
                                                         orders
  ------------------------------------------------------------------------------

All endpoints require appropriate authentication, authorization, input
validation, tenant scoping, rate limits and audit logging. Telephony
webhook signatures must be verified according to the provider's
documented mechanism.

------------------------------------------------------------------------

## 12. Invoice and billing design

The billing engine, not the LLM, calculates:

-   Product line totals
-   Subtotal
-   Applicable tax
-   Discounts
-   Delivery or other configured charges
-   Final payable amount

The invoice should include:

-   Shop name and contact details
-   Invoice number and date
-   Customer details where appropriate
-   Product names in the selected language
-   Quantity and unit
-   Unit price
-   Line total
-   Tax and discount breakdown
-   Grand total
-   Applicable legal invoice fields based on the shop's registration and
    tax requirements

Tax treatment must be configurable and reviewed for the actual retailer
and jurisdiction. Do not assume that every small shop has the same GST
obligations.

Localized invoice output should use fonts that correctly render Kannada,
Telugu, Tamil and Malayalam scripts.

------------------------------------------------------------------------

## 13. Security, privacy and reliability

### Security

-   HTTPS and secure WebSockets
-   Authenticated and authorized APIs
-   Role-based shopkeeper access
-   Strict tenant-level data isolation
-   Secrets stored outside source control
-   Database backups and encryption at rest where supported
-   Webhook signature verification
-   Input validation and output encoding
-   Dependency and container vulnerability scanning
-   Sensitive-data redaction in logs

### Privacy

-   Give appropriate notice and obtain required consent before recording
    calls.
-   Define retention periods for audio, transcripts and customer data.
-   Avoid retaining raw audio unless there is a justified and disclosed
    need.
-   Restrict access to customer information and invoices.
-   Review applicable Indian privacy and telecom requirements before
    launch.
-   Document provider data processing and cross-border data handling.

### Reliability

-   Idempotent order and billing operations
-   Database transactions for order confirmation
-   Retry policies for temporary provider failures
-   Separate invoice delivery status from order status
-   Persist conversation state
-   Handle duplicate and out-of-order telephony webhooks
-   Health checks, structured logs and operational alerts

------------------------------------------------------------------------

## 14. Monitoring and evaluation

Measure the system independently for each language.

  Metric                                            Initial acceptance target
  ----------------------------------------------- ---------------------------
  Product extraction accuracy                                    At least 95%
  Quantity extraction accuracy                                   At least 98%
  Billing calculation accuracy                                           100%
  Duplicate confirmed orders                                                0
  Correct language response                                      At least 98%
  Explicit confirmation before order submission                          100%
  Successful required human handoff                              At least 95%

These are proposed targets. Build a representative test set and measure
actual performance before making product claims.

Additional operational metrics:

-   Call answer success rate
-   Average call duration
-   ASR latency
-   Time to first AI response
-   End-to-end order completion time
-   Product matching clarification rate
-   Call abandonment rate
-   Human handoff rate
-   Invoice delivery success rate
-   Cost per completed order
-   Provider errors and service availability

------------------------------------------------------------------------

## 15. Local development setup

### Prerequisites

Install:

-   Git
-   Python 3.11 or a project-compatible newer version
-   PostgreSQL
-   Docker Desktop or Docker Engine with Compose
-   Node.js LTS (for the future React dashboard)

Optional integrations will require provider accounts and credentials:

-   Telephony provider
-   ASR provider
-   LLM provider
-   TTS provider
-   WhatsApp Business provider

Do not commit API keys, phone credentials, database passwords or
provider secrets to Git.

### Clone the repository

Once the project repository has been created:

``` bash
git clone <YOUR_GIT_REPOSITORY_URL>
cd bharatvoice-ai
```

### Create a Python virtual environment

macOS / Linux:

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

``` powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Install backend dependencies

After `backend/requirements.txt` has been created:

``` bash
pip install -r backend/requirements.txt
```

### Configure environment variables

Create a local environment file from the example:

``` bash
cp .env.example .env
```

Illustrative variables to be defined as implementation proceeds:

``` dotenv
APP_ENV=development
APP_NAME=BharatVoice AI

DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/bharatvoice
REDIS_URL=redis://localhost:6379/0

DEFAULT_LANGUAGE=kn
SUPPORTED_LANGUAGES=kn,te,ta,ml

LLM_PROVIDER=
LLM_API_KEY=

ASR_PROVIDER=
ASR_API_KEY=

TTS_PROVIDER=
TTS_API_KEY=

TELEPHONY_PROVIDER=
TELEPHONY_API_KEY=

WHATSAPP_PROVIDER=
WHATSAPP_API_KEY=
```

Replace placeholders with actual credentials only in the local `.env`
file or a secret manager. The example values are configuration
placeholders, not working credentials.

### Start the application

The following commands are intended for the implementation stage, once
the relevant files and services exist.

``` bash
docker compose up --build
```

For a locally implemented FastAPI backend:

``` bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API documentation should be available at:

``` text
http://localhost:8000/docs
```

### Run tests

Once tests are implemented:

``` bash
pytest -q
```

------------------------------------------------------------------------

## 16. Development roadmap

  -----------------------------------------------------------------------
  Phase                               Deliverable
  ----------------------------------- -----------------------------------
  1\. Foundation                      Git repository, FastAPI skeleton,
                                      configuration and PostgreSQL

  2\. Catalogue                       Shop registration, products,
                                      regional aliases, prices and
                                      inventory

  3\. Orders                          Draft orders, product validation,
                                      modifications and confirmation

  4\. Billing                         Deterministic billing, tax
                                      configuration and invoice
                                      generation

  5\. Multilingual AI                 ASR, LLM extraction, language
                                      context and TTS for four languages

  6\. Telephony                       Incoming calls, IVR, streaming
                                      audio and human handoff

  7\. Notifications                   WhatsApp/SMS invoice delivery

  8\. Dashboard                       Shopkeeper order, catalogue and
                                      escalation management

  9\. Production readiness            Language evaluation, security, load
                                      testing, monitoring and deployment
  -----------------------------------------------------------------------

### Recommended implementation order

Start with the catalogue and order-processing engine. Test the full
order lifecycle using typed sample requests before connecting external
speech and telephony providers. This keeps the business logic testable
and avoids spending time debugging AI, telecom and billing issues
simultaneously.

------------------------------------------------------------------------

## 17. Future expansion

After the Kirana ordering MVP is stable, the shared platform can
support:

### Civic services

-   Voice-based public service requests
-   Local-language issue reporting
-   Request status tracking
-   Human escalation to the relevant department

### Learning and education

-   Local-language learning assistant
-   Voice-based question answering
-   Reading and pronunciation assistance
-   Guided educational exercises

These should be separate domain workflows built on the shared
communication and multilingual AI foundation. The commerce catalogue and
billing logic should not be coupled to them.

------------------------------------------------------------------------

## 18. Project acceptance checklist

### Foundation

-   [ ] Repository and contribution conventions established
-   [ ] Backend skeleton created
-   [ ] Database migrations configured
-   [ ] Shop and user authentication implemented
-   [ ] Tenant isolation tested

### Commerce

-   [ ] Sample shop and product catalogue loaded
-   [ ] Regional aliases supported
-   [ ] Inventory validation implemented
-   [ ] Draft order lifecycle implemented
-   [ ] Explicit order confirmation enforced
-   [ ] Billing calculations tested
-   [ ] Invoice generation tested

### Multilingual AI

-   [ ] Kannada evaluation set prepared
-   [ ] Telugu evaluation set prepared
-   [ ] Tamil evaluation set prepared
-   [ ] Malayalam evaluation set prepared
-   [ ] ASR and TTS benchmarked
-   [ ] Product extraction and quantity evaluation completed
-   [ ] Code-switching and noisy-audio scenarios tested

### Voice and operations

-   [ ] Incoming call integration tested
-   [ ] Language IVR tested
-   [ ] Audio streaming tested
-   [ ] Call disconnection and recovery tested
-   [ ] Human handoff tested
-   [ ] WhatsApp/SMS delivery tested
-   [ ] Dashboard and monitoring implemented
-   [ ] Privacy, security and production reviews completed

------------------------------------------------------------------------

## 19. Project success definition

The MVP is successful when a customer can call a participating shop,
select Kannada, Telugu, Tamil or Malayalam, place a grocery order
entirely by voice, correct it, hear the complete order and price,
explicitly confirm it, and receive an accurate localized invoice ---
while the shopkeeper only intervenes when the system needs help.

**BharatVoice AI: Speak in your language. Order with confidence.**
