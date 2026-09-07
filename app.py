import io
import os
import re
import socket
import ipaddress
import zipfile
from urllib.parse import urljoin, urlparse, quote

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

APP_USER_AGENT = "PaperFetcher/1.0 (+https://example.org; lawful-open-access-fetcher)"
REQUEST_TIMEOUT = 25
MAX_PDF_MB = 80

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


def normalize_doi(value: str):
    value = value.strip()
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    m = DOI_RE.search(value)
    return m.group(0).rstrip(".,;)") if m else None


def split_inputs(text: str):
    # One item per line is best, but comma/semicolon-separated values also work.
    raw = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = re.split(r"[\t,;]+", line)
        raw.extend(p.strip() for p in parts if p.strip())
    # Keep order, drop duplicates.
    seen, out = set(), []
    for x in raw:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def is_public_host(hostname: str):
    if not hostname:
        return False
    host = hostname.lower().strip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False
    return True


def validate_public_url(url: str):
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed.")
    if not is_public_host(p.hostname):
        raise ValueError("URL does not resolve to a public Internet host.")
    return url


def safe_request(session, method, url, *, headers=None, stream=False, max_redirects=8, **kwargs):
    current = validate_public_url(url)
    for _ in range(max_redirects + 1):
        resp = session.request(
            method,
            current,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=stream,
            **kwargs,
        )
        if resp.status_code in {301, 302, 303, 307, 308}:
            loc = resp.headers.get("Location")
            if not loc:
                return resp
            current = validate_public_url(urljoin(current, loc))
            continue
        return resp
    raise requests.TooManyRedirects("Too many redirects.")


def content_type(resp):
    return (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()


def looks_like_pdf_bytes(data: bytes):
    return data[:5] == b"%PDF-"


def sanitize_filename(name: str, fallback="paper.pdf"):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if not name:
        name = fallback
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:180]


def title_from_crossref(message):
    title = ""
    if isinstance(message.get("title"), list) and message["title"]:
        title = message["title"][0]
    return title.strip()


def crossref_lookup(session, doi, email=""):
    headers = {"User-Agent": APP_USER_AGENT}
    params = {}
    if email:
        params["mailto"] = email
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    r = safe_request(session, "GET", url, headers=headers, params=params)
    if r.status_code != 200:
        return {}
    try:
        return r.json().get("message", {})
    except Exception:
        return {}


def unpaywall_lookup(session, doi, email):
    if not email:
        return {}
    url = "https://api.unpaywall.org/v2/" + quote(doi, safe="")
    r = safe_request(
        session,
        "GET",
        url,
        headers={"User-Agent": APP_USER_AGENT},
        params={"email": email},
    )
    if r.status_code != 200:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def candidate_urls_from_unpaywall(data):
    candidates = []
    best = data.get("best_oa_location") or {}
    for key in ("url_for_pdf", "url"):
        u = best.get(key)
        if u:
            candidates.append(("Unpaywall OA", u))

    for loc in data.get("oa_locations") or []:
        for key in ("url_for_pdf", "url"):
            u = loc.get(key)
            if u:
                candidates.append(("Unpaywall OA", u))
    return dedupe_candidates(candidates)


def candidate_urls_from_crossref(message):
    candidates = []
    for link in message.get("link") or []:
        u = link.get("URL")
        ctype = (link.get("content-type") or "").lower()
        if u and ("pdf" in ctype or u.lower().split("?")[0].endswith(".pdf")):
            candidates.append(("Crossref full-text link", u))
    resource = message.get("resource") or {}
    primary = resource.get("primary") or {}
    if primary.get("URL"):
        candidates.append(("Crossref landing page", primary["URL"]))
    if message.get("URL"):
        candidates.append(("DOI landing page", message["URL"]))
    return dedupe_candidates(candidates)


def dedupe_candidates(candidates):
    seen, out = set(), []
    for source, u in candidates:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append((source, u))
    return out


def html_pdf_candidates(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out = []

    meta_names = [
        "citation_pdf_url",
        "wkhealth_pdf_url",
        "eprints.document_url",
    ]
    for meta_name in meta_names:
        tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(meta_name)}$", re.I)})
        if tag and tag.get("content"):
            out.append(("Landing-page PDF metadata", urljoin(base_url, tag["content"])))

    # Common meta/property variants.
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").lower()
        val = tag.get("content")
        if val and ("pdf" in key) and ("url" in key or "citation" in key):
            out.append(("Landing-page PDF metadata", urljoin(base_url, val)))

    # Conservative link fallback: only links that clearly look like PDF/full-text downloads.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = " ".join(a.stripped_strings).lower()
        href_l = href.lower()
        if (
            href_l.split("?")[0].endswith(".pdf")
            or "download pdf" in text
            or text == "pdf"
            or "full text pdf" in text
        ):
            out.append(("Landing-page PDF link", urljoin(base_url, href)))

    return dedupe_candidates(out)[:12]


def fetch_pdf(session, url):
    headers = {
        "User-Agent": APP_USER_AGENT,
        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    }
    r = safe_request(session, "GET", url, headers=headers, stream=True)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}", r.url

    clen = r.headers.get("Content-Length")
    if clen:
        try:
            if int(clen) > MAX_PDF_MB * 1024 * 1024:
                return None, f"PDF exceeds {MAX_PDF_MB} MB limit", r.url
        except ValueError:
            pass

    ctype = content_type(r)
    chunks = []
    total = 0
    for chunk in r.iter_content(chunk_size=1024 * 256):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_PDF_MB * 1024 * 1024:
            return None, f"PDF exceeds {MAX_PDF_MB} MB limit", r.url
    data = b"".join(chunks)

    if "pdf" in ctype or looks_like_pdf_bytes(data):
        if looks_like_pdf_bytes(data):
            return data, "PDF", r.url
    return None, f"Not a PDF ({ctype or 'unknown content type'})", r.url


def inspect_landing_page(session, url):
    headers = {
        "User-Agent": APP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.7",
    }
    r = safe_request(session, "GET", url, headers=headers)
    if r.status_code != 200:
        return [], f"Landing page HTTP {r.status_code}"

    ctype = content_type(r)
    if "pdf" in ctype or looks_like_pdf_bytes(r.content):
        return [("Direct PDF", r.url)], None
    if "html" not in ctype and not r.text.lstrip().startswith("<"):
        return [], f"Unsupported landing content: {ctype or 'unknown'}"
    return html_pdf_candidates(r.text, r.url), None


def filename_for(title, doi, final_url, index):
    if title:
        return sanitize_filename(title)
    if doi:
        return sanitize_filename(doi.replace("/", "_"))
    path = urlparse(final_url).path
    base = os.path.basename(path)
    if base.lower().endswith(".pdf") and len(base) > 4:
        return sanitize_filename(base)
    return f"paper_{index:03d}.pdf"


def unique_filename(name, used):
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while f"{stem}_{i}{ext}" in used:
        i += 1
    out = f"{stem}_{i}{ext}"
    used.add(out)
    return out


def resolve_and_download(session, item, email, index):
    result = {
        "input": item,
        "doi": "",
        "title": "",
        "status": "Failed",
        "source": "",
        "final_url": "",
        "message": "",
        "filename": "",
        "bytes": None,
    }

    doi = normalize_doi(item)
    result["doi"] = doi or ""

    candidates = []
    crossref = {}

    if doi:
        crossref = crossref_lookup(session, doi, email)
        result["title"] = title_from_crossref(crossref)

        # Prefer verified open-access locations.
        upw = unpaywall_lookup(session, doi, email)
        candidates.extend(candidate_urls_from_unpaywall(upw))
        candidates.extend(candidate_urls_from_crossref(crossref))

        # DOI resolver is a final metadata/landing-page fallback.
        candidates.append(("DOI resolver", "https://doi.org/" + doi))
    else:
        parsed = urlparse(item)
        if parsed.scheme in {"http", "https"}:
            candidates.append(("Submitted URL", item))
        else:
            result["message"] = "Input is neither a recognizable DOI nor an http(s) URL."
            return result

    candidates = dedupe_candidates(candidates)
    attempted = []

    for source, url in candidates:
        try:
            # First try URL as a PDF directly.
            data, why, final_url = fetch_pdf(session, url)
            attempted.append(f"{source}: {why}")
            if data:
                result.update(
                    status="Downloaded",
                    source=source,
                    final_url=final_url,
                    message="PDF retrieved successfully.",
                    bytes=data,
                )
                return result

            # If not a PDF, inspect it as an HTML landing page.
            landing_candidates, landing_err = inspect_landing_page(session, url)
            if landing_err:
                continue

            for subsource, pdf_url in landing_candidates:
                try:
                    data2, why2, final_url2 = fetch_pdf(session, pdf_url)
                    attempted.append(f"{subsource}: {why2}")
                    if data2:
                        result.update(
                            status="Downloaded",
                            source=subsource,
                            final_url=final_url2,
                            message="PDF retrieved successfully.",
                            bytes=data2,
                        )
                        return result
                except Exception as e:
                    attempted.append(f"{subsource}: {type(e).__name__}")
        except Exception as e:
            attempted.append(f"{source}: {type(e).__name__}")

    if doi and not email:
        result["message"] = (
            "No downloadable PDF found. Add a contact email to enable Unpaywall open-access lookup. "
            "The app does not bypass publisher paywalls or authentication."
        )
    else:
        result["message"] = (
            "No publicly downloadable PDF was found from the submitted/metadata URLs. "
            "The paper may require subscription/login, JavaScript, or a publisher-specific API."
        )
    if attempted:
        result["message"] += " Tried: " + "; ".join(attempted[-6:])
    return result


st.set_page_config(page_title="Paper Fetcher", page_icon="📄", layout="wide")

st.title("Paper Fetcher")
st.caption(
    "Submit one or many journal-paper URLs/DOIs. The app fetches direct PDFs and openly available copies, "
    "then bundles successful downloads into a ZIP."
)

with st.expander("Access policy", expanded=False):
    st.write(
        "This tool respects publisher access controls. It does not bypass paywalls, CAPTCHAs, logins, "
        "robots/access restrictions, or DRM. Only use it for papers you are legally entitled to download."
    )

left, right = st.columns([2, 1])

with left:
    raw = st.text_area(
        "URLs or DOIs",
        height=240,
        placeholder=(
            "10.1038/s41586-020-2649-2\n"
            "https://doi.org/10.xxxx/xxxxx\n"
            "https://example.org/article.pdf"
        ),
        help="Use one item per line. Comma/semicolon-separated values also work.",
    )

with right:
    email = st.text_input(
        "Contact email (recommended)",
        value=os.getenv("UNPAYWALL_EMAIL", ""),
        help="Used for Unpaywall and Crossref polite API access. It is not saved by this app.",
    )
    max_items = st.number_input("Maximum items per run", 1, 200, 50, 1)
    st.info(
        "For best results, paste DOIs. Direct PDF URLs are also supported. "
        "Some publisher pages require browser login/JavaScript and cannot be fetched server-side."
    )

run = st.button("Fetch papers", type="primary", use_container_width=True)

if run:
    items = split_inputs(raw)
    if not items:
        st.warning("Enter at least one DOI or URL.")
        st.stop()
    if len(items) > max_items:
        st.warning(f"You entered {len(items)} items; only the first {max_items} will be processed.")
        items = items[: int(max_items)]

    session = requests.Session()
    session.headers.update({"User-Agent": APP_USER_AGENT})

    progress = st.progress(0)
    status_box = st.empty()
    results = []
    used_names = set()

    for i, item in enumerate(items, 1):
        status_box.write(f"Processing {i}/{len(items)}: `{item}`")
        res = resolve_and_download(session, item, email.strip(), i)
        if res["bytes"]:
            name = filename_for(res["title"], res["doi"], res["final_url"], i)
            res["filename"] = unique_filename(name, used_names)
        results.append(res)
        progress.progress(i / len(items))

    status_box.empty()

    table = pd.DataFrame(
        [
            {
                "#": i,
                "Input": r["input"],
                "DOI": r["doi"],
                "Title": r["title"],
                "Status": r["status"],
                "Source": r["source"],
                "File": r["filename"],
                "Message": r["message"],
            }
            for i, r in enumerate(results, 1)
        ]
    )

    downloaded = [r for r in results if r["bytes"]]
    c1, c2, c3 = st.columns(3)
    c1.metric("Submitted", len(results))
    c2.metric("Downloaded", len(downloaded))
    c3.metric("Not downloaded", len(results) - len(downloaded))

    st.dataframe(table, use_container_width=True, hide_index=True)

    if downloaded:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for r in downloaded:
                zf.writestr(r["filename"], r["bytes"])
            zf.writestr(
                "download_report.csv",
                table.drop(columns=["Message"]).to_csv(index=False).encode("utf-8"),
            )

        st.download_button(
            "Download all successful papers as ZIP",
            data=zip_buffer.getvalue(),
            file_name="papers.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )

        with st.expander("Individual PDFs"):
            for r in downloaded:
                st.download_button(
                    label=f"Download {r['filename']}",
                    data=r["bytes"],
                    file_name=r["filename"],
                    mime="application/pdf",
                    key="dl_" + r["filename"],
                )
