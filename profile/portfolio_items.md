# Freelancer Portfolio Items

> **Published 2026-11-23** (Freelancer portfolio IDs): ReviewLens 16136944,
> LaunchScout 16136950, DataPilot 16136958. Each has its correct screenshot.

Three real projects, described for the profile's focus (Python, data, scraping).
Title max 36 characters, description max 2,000. No external links (Freelancer
doesn't allow off-site contact details).

---

## 1. ReviewLens: Customer Review Analyzer

**Screenshot:** `C:\Users\HP\OneDrive\Desktop\Project\reviewlens\docs\screenshots\03_dashboard.png`

**Title (29):** `ReviewLens: Review Analyzer`

**Description:**
```
A Python tool that turns hundreds of raw customer reviews into clean, tagged data and a weekly report a manager can act on in five minutes.

What it does:
- Reads a weekly CSV of reviews (Google, Zomato, Swiggy, Amazon), drops empty and duplicate rows, and validates the columns before processing.
- Tags every review with sentiment, category (food quality, delivery, price, staff, packaging, app), urgency and language, including Tamil and Tanglish.
- Counts everything with pandas, so the numbers in the report always match the data.
- Shows a dashboard with charts and a high-urgency filter, compares with last week, and exports an Excel-ready CSV for the support team.
- Emails the weekly report automatically every Monday.

Built with Python, pandas, Plotly, Streamlit and an LLM (LangChain + OpenAI) for tagging. Tested with pytest. Deployed and running live.
```

**Tags:** `data cleaning, csv, excel, dashboard, report automation, customer reviews`
**Tools:** `Python, pandas, Plotly, Streamlit, LangChain, OpenAI`
**Skills:** `Python, Data Processing, Data Cleansing, Excel, Automation`
**Industry:** `Restaurants & e-commerce`

---

## 2. LaunchScout: Competitor Research

**Screenshot:** `C:\Users\HP\OneDrive\Desktop\Project\launchscout\docs\screenshots\06_key_competitors.png`

**Title (31):** `LaunchScout: Competitor Research`

**Description:**
```
An automated research tool that searches the web, reads competitor websites and reviews, and produces a sourced competitor report for any business idea and city.

What it does:
- Finds 3-5 real competitors with prices, offers and sales channels, each with its source URL.
- Collects what customers like and complain about from public pages and Google Maps reviews.
- Builds a comparison table, identifies the market gap and recommends pricing and positioning.
- Delivers the report in the app and as Markdown and PDF downloads, with a quality checklist.

Example run: home-delivered millet meal boxes in Coimbatore. A second run for at-home pet grooming in Bengaluru found 5 competitors with every price verified and 9 reviews cited, with no code changes.

Built with Python, requests, BeautifulSoup (web scraping), a search API, CrewAI agents and Streamlit. Deployed and running live.
```

**Tags:** `web scraping, market research, competitor analysis, data extraction, pdf report`
**Tools:** `Python, BeautifulSoup, requests, CrewAI, Streamlit`
**Skills:** `Python, Web Scraping, Data Scraping, Automation`
**Industry:** `Small business & startups`

---

## 3. DataPilot: AI Data Analyst

**Screenshot:** `C:\Users\HP\OneDrive\Desktop\Project\datapilot\docs\screenshots\06_final_answer.jpg`

**Title (25):** `DataPilot: AI Data Analyst`

**Description:**
```
Upload a spreadsheet, ask a business question in plain English, and get a checked answer with charts, a re-runnable Python script and a one-page PDF.

How it works:
- A planner reads only the column structure (never the full data) and writes a step-by-step analysis plan.
- Python code (pandas + matplotlib) is written and run in a sandbox; if it errors, it is fixed and re-run automatically.
- A reviewer checks every part of the answer against the printed numbers and chart labels before it is approved.
- You get the answer, the charts, the analysis.py script you can rerun on new data, and a PDF summary.

Works with Excel and CSV files. Built with Python, pandas, matplotlib, openpyxl, fpdf2, AutoGen and Streamlit. Deployed and running live.
```

**Tags:** `data analysis, excel, csv, python automation, charts, pdf report`
**Tools:** `Python, pandas, matplotlib, openpyxl, AutoGen, Streamlit`
**Skills:** `Python, Excel, Data Processing, Automation`
**Industry:** `Business analytics`
