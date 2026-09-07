from pathlib import Path
import re, py_compile, zipfile

base = Path("/mnt/data/paper_fetcher_webapp")
app_path = base / "app.py"
text = app_path.read_text(encoding="utf-8")

# Insert persistent state initialization after Streamlit page config.
old = '''st.set_page_config(page_title="Paper Fetcher", page_icon="📄", layout="wide")

st.title("Paper Fetcher")
'''
new = '''st.set_page_config(page_title="Paper Fetcher", page_icon="📄", layout="wide")

# Persist completed fetch results across Streamlit reruns.
if "fetch_results" not in st.session_state:
    st.session_state.fetch_results = None
if "fetch_table" not in st.session_state:
    st.session_state.fetch_table = None
if "papers_zip" not in st.session_state:
    st.session_state.papers_zip = None

st.title("Paper Fetcher")
'''
if old not in text:
    raise RuntimeError("Could not find page-config block to patch.")
text = text.replace(old, new, 1)

# Replace the entire run/result rendering block with a stateful version.
start = text.index('if run:\n')
replacement = r'''if run:
    # Clear previous results when starting a new fetch.
    st.session_state.fetch_results = None
    st.session_state.fetch_table = None
    st.session_state.papers_zip = None

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

    # Build ZIP once and persist it in session state so it survives button clicks/reruns.
    zip_bytes = None
    if downloaded:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for r in downloaded:
                zf.writestr(r["filename"], r["bytes"])
            zf.writestr(
                "download_report.csv",
                table.drop(columns=["Message"]).to_csv(index=False).encode("utf-8"),
            )
        zip_bytes = zip_buffer.getvalue()

    st.session_state.fetch_results = results
    st.session_state.fetch_table = table
    st.session_state.papers_zip = zip_bytes


# Render completed results OUTSIDE the Fetch button condition.
# This keeps them available after any later widget interaction.
if st.session_state.fetch_results is not None:
    results = st.session_state.fetch_results
    table = st.session_state.fetch_table
    downloaded = [r for r in results if r.get("bytes")]

    c1, c2, c3 = st.columns(3)
    c1.metric("Submitted", len(results))
    c2.metric("Downloaded", len(downloaded))
    c3.metric("Not downloaded", len(results) - len(downloaded))

    st.dataframe(table, use_container_width=True, hide_index=True)

    if downloaded and st.session_state.papers_zip:
        zip_size_mb = len(st.session_state.papers_zip) / (1024 * 1024)
        st.caption(f"ZIP ready: {len(downloaded)} PDF(s), {zip_size_mb:.1f} MB")

        st.download_button(
            "Download all successful papers as ZIP",
            data=st.session_state.papers_zip,
            file_name="papers.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
            key="download_all_zip",
            on_click="ignore",
        )

        with st.expander("Individual PDFs"):
            for i, r in enumerate(downloaded, 1):
                st.download_button(
                    label=f"Download {r['filename']}",
                    data=r["bytes"],
                    file_name=r["filename"],
                    mime="application/pdf",
                    key=f"dl_pdf_{i}_{r['filename']}",
                    on_click="ignore",
                    use_container_width=True,
                )

        if zip_size_mb > 180:
            st.warning(
                "This ZIP is quite large. Streamlit keeps download data in memory, "
                "so very large batches can exceed Community Cloud memory limits. "
                "If needed, fetch the papers in smaller batches."
            )
'''
text = text[:start] + replacement + "\n"

app_path.write_text(text, encoding="utf-8")
py_compile.compile(str(app_path), doraise=True)

# Rebuild the downloadable package.
zip_path = Path("/mnt/data/paper_fetcher_webapp_fixed.zip")
include = [
    "app.py", "requirements.txt", "README.md", "Dockerfile",
    ".env.example", "run_windows.bat", "run_mac_linux.sh"
]
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for name in include:
        z.write(base / name, arcname=f"paper_fetcher_webapp/{name}")

print("Fixed app compiled successfully.")
print(f"Updated app.py: {app_path}")
print(f"Updated package: {zip_path}")
