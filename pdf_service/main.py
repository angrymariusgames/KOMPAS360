import os
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    # Lad appen advare tydeligt – runtime-kald vil kaste en fejl
    print("WARNING: SUPABASE_URL eller SUPABASE_SERVICE_KEY mangler. /api-endpoints vil fejle ved runtime.")

def sb() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ------------------------------------------------------------
# App
# ------------------------------------------------------------
app = FastAPI(title="KOMPAS Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # Stram evt. til specifikke origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# Models
# ------------------------------------------------------------
class ColleagueSupportRequest(BaseModel):
    project_id: str
    competencies: List[str]
    target_id: Optional[str] = None  # udelad målpersonen i forslag

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/")
def root():
    """Server admin.html som root for nem testning."""
    if not os.path.exists("admin.html"):
        raise HTTPException(404, "admin.html ikke fundet i projektmappen")
    return FileResponse("admin.html")

@app.post("/api/colleague-support")
def get_colleague_support(data: ColleagueSupportRequest):
    """
    Find én unik kollega pr. kompetence (ingen gennemsnit).
    Returnerer op til |competencies| entries, hver med {full_name, competency}.
    """
    try:
        s = sb()

        # Navneopslag
        people = (
            s.table("project_people")
             .select("id,full_name")
             .eq("project_id", data.project_id)
             .execute()
             .data or []
        )
        if not people:
            return []
        name_by_id = {p["id"]: p["full_name"] for p in people}

        # Scores for de valgte kompetencer
        rows = (
            s.table("v_kompas_series")
             .select("target_id,category_id,category_name,safe_avg")
             .eq("project_id", data.project_id)
             .in_("category_id", data.competencies)
             .execute()
             .data or []
        )
        rows = [r for r in rows if r.get("safe_avg") is not None and (not data.target_id or r["target_id"] != data.target_id)]

        # Kandidater pr. kompetence
        from collections import defaultdict
        cand = defaultdict(list)  # comp_id -> [{tid, name, score, comp_name}]
        for r in rows:
            cand[r["category_id"]].append({
                "tid": r["target_id"],
                "name": name_by_id.get(r["target_id"], "(ukendt)"),
                "score": float(r["safe_avg"]),
                "comp_name": r["category_name"],
            })
        for cid in list(cand.keys()):
            cand[cid].sort(key=lambda x: (-x["score"], x["name"]))

        # Greedy: gennemgå kompetencer i den rækkefølge klienten sendte dem,
        # vælg første kandidat der ikke allerede er brugt.
        used = set()
        out = []
        for cid in data.competencies:
            lst = cand.get(cid, [])
            pick = next((c for c in lst if c["tid"] not in used), None)
            if pick:
                used.add(pick["tid"])
                out.append({
                    "full_name": pick["name"],
                    "competency": pick["comp_name"]
                })

        return out

    except Exception as ex:
        print(f"Error in /api/colleague-support: {ex}")
        return []

# --------------------------------------------------------------------
# PDF batch-rendering (backend) – render HTML -> PDF / ZIP med WeasyPrint
# --------------------------------------------------------------------
from pydantic import BaseModel
from typing import List, Optional

class PdfItem(BaseModel):
    filename: str
    html: str

class PdfRenderRequest(BaseModel):
    mode: str  # 'combined' | 'separate'
    title: Optional[str] = None
    items: List[PdfItem]

@app.post("/api/pdf/render")
def render_pdf_batch(req: PdfRenderRequest):
    """
    Modtager en liste af {filename, html}. Hvis mode == 'combined', laves én samlet PDF
    (alle HTML-sektioner adskilt af side-skift). Hvis mode == 'separate', laves én PDF pr. item
    og returneres som ZIP.
    """
    try:
        # Imports her for at undgå top-level ændringer
        import io, zipfile, re
        from fastapi import HTTPException
        from fastapi.responses import StreamingResponse
        from weasyprint import HTML

        def safe(s: Optional[str]) -> str:
            s = s or ""
            s = re.sub(r"\s+", "_", s)
            s = re.sub(r"[^A-Za-z0-9_\-\.]", "", s)
            return s

        if req.mode not in ("combined", "separate"):
            raise HTTPException(status_code=400, detail="mode must be 'combined' or 'separate'")

        if not req.items:
            raise HTTPException(status_code=400, detail="items must be non-empty")

        # (valgfrie) sikkerhedsgrænser
        if len(req.items) > 500:
            raise HTTPException(status_code=413, detail="Too many items in one request")

        if req.mode == "combined":
            # Saml alle HTML-sektioner i én HTML med page-break mellem hver
            pages = []
            for it in req.items:
                # Wrap hver HTML i en container som fylder en A4-side i WeasyPrint
                pages.append(f"""
                <section style="page-break-after: always;">
                {it.html}
                </section>
                """)
            full_html = f"""
            <html><head>
              <meta charset="utf-8">
              <style>
                @page {{ size: A4; margin: 12mm; }}
                body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif; }}
                section:last-child {{ page-break-after: auto; }}
              </style>
            </head>
            <body>
              {''.join(pages)}
            </body></html>
            """
            pdf_bytes = HTML(string=full_html).write_pdf()
            title = safe(req.title or "resultater")
            buf = io.BytesIO(pdf_bytes)
            headers = {"Content-Disposition": f'attachment; filename="{title}.pdf"'}
            return StreamingResponse(buf, media_type="application/pdf", headers=headers)

        # separate -> ZIP med én PDF pr. item
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
            for it in req.items:
                html = f"""
                <html><head>
                  <meta charset="utf-8">
                  <style>
                    @page {{ size: A4; margin: 12mm; }}
                    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif; }}
                  </style>
                </head>
                <body>{it.html}</body></html>
                """
                pdf_bytes = HTML(string=html).write_pdf()
                fname = safe(it.filename or "profil") + ".pdf"
                zf.writestr(fname, pdf_bytes)
        mem.seek(0)
        title = safe(req.title or "resultater")
        headers = {"Content-Disposition": f'attachment; filename="{title}.zip"'}
        return StreamingResponse(mem, media_type="application/zip", headers=headers)

    except HTTPException:
        raise
    except Exception as ex:
        print("PDF render error:", ex)
        raise HTTPException(status_code=500, detail="PDF rendering failed")


