import streamlit as st
from pathlib import Path
import tempfile

from config import (
    REPORTS_DIR,
    MODEL_NAME,
    MAX_FILE_SIZE_MB,
)
from agent.revenue_agent import DeepRevenueAgent

# Page configuration
st.set_page_config(
    page_title="Deep Revenue Agent",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Deep Revenue Agent")
st.caption(
    "Autonomous Financial Deep Agent powered by **deepagents**, **PyMuPDF**, "
    f"**Ollama ({MODEL_NAME})**. Zero Vectors. Zero Embeddings."
)

# Sidebar: Configuration & Document Selection
st.sidebar.header("📁 Document Selection")
input_option = st.sidebar.radio(
    "Document Source:",
    ("Choose from existing reports", "Upload new PDF / DOCX"),
)

selected_file_path = None

if input_option == "Choose from existing reports":
    available_reports = sorted([f.name for f in REPORTS_DIR.glob("*.*") if f.suffix.lower() in [".pdf", ".docx"]])
    if available_reports:
        selected_filename = st.sidebar.selectbox("Select Report:", available_reports)
        selected_file_path = REPORTS_DIR / selected_filename
    else:
        st.sidebar.warning("No reports found in data/reports/")
else:
    uploaded_file = st.sidebar.file_uploader("Upload Company Report (PDF or DOCX)", type=["pdf", "docx"])
    if uploaded_file is not None:
        upload_bytes = uploaded_file.getbuffer()
        if len(upload_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
            st.sidebar.error(f"File exceeds the {MAX_FILE_SIZE_MB} MB upload limit.")
        else:
            temp_dir = Path(tempfile.gettempdir()) / "revenue_agent_uploads"
            temp_dir.mkdir(parents=True, exist_ok=True)
            safe_name = Path(uploaded_file.name).name
            temp_path = temp_dir / safe_name
            with open(temp_path, "wb") as f:
                f.write(upload_bytes)
            selected_file_path = temp_path
            st.sidebar.success(f"Uploaded: {safe_name}")

# Main Execution Area
if selected_file_path:
    st.subheader(f"📄 Target Document: `{selected_file_path.name}`")
    
    col1, col2 = st.columns([1, 4])
    with col1:
        run_button = st.button("🚀 Run Deep Agent", type="primary", use_container_width=True)

    if run_button:
        with st.status("🤖 Deep Agent researching document...", expanded=True) as status:
            try:
                st.write("🔍 **Autonomous Navigation**: Inspecting bookmarks and locating likely statement pages...")
                agent = DeepRevenueAgent()
                
                st.write("📖 **PyMuPDF Extraction**: Reading structured Markdown tables (with column dates) and page text...")
                st.write("📋 **Extraction Summary**: Preparing revenue figures and source citations...")
                
                result = agent.extract_revenue(str(selected_file_path))
                
                status.update(
                    label=f"✅ Extraction complete for {result.company_name or selected_file_path.name}",
                    state="complete",
                    expanded=False,
                )

                # Render Highlights
                st.success(f"Revenue extracted for **{result.company_name or selected_file_path.name}**")
                
                # Metric Cards
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric(result.matched_line or "Reported Revenue", f"{result.currency or ''} {result.reported_revenue or 'N/A'}")
                m2.metric("Total Income", f"{result.currency or ''} {result.total_income or 'N/A'}")
                m3.metric("Fiscal Year", result.fiscal_year or "N/A")
                m4.metric("Unit / Scale", result.unit_scale or "As reported")
                m5.metric("Scope", (result.scope or "").upper())

                st.markdown("### 📄 Source details")
                st.markdown(f"- **Matched Line Item**: `{result.matched_line or 'N/A'}`")
                st.markdown(f"- **Source Page(s)**: `{result.source_pages or 'N/A'}`")

                if result.additional_revenue_lines:
                    st.markdown("### Other reported revenue lines")
                    for item in result.additional_revenue_lines:
                        st.markdown(
                            f"- **{item.label}**: {item.amount} "
                            f"(pages: {item.source_pages or 'DOCX'})"
                        )
                        if item.supporting_evidence:
                            st.code(item.supporting_evidence, language="markdown")

                # Verbatim Evidence & Notes
                with st.expander("🔎 Verbatim Supporting Evidence from Statement", expanded=True):
                    st.code(result.supporting_evidence or "No verbatim snippet recorded.", language="markdown")

            except Exception as e:
                status.update(label="❌ Agent Execution Error", state="error")
                st.error(f"Error during deep agent execution: {e}")
                st.info("Make sure Ollama is running (`ollama serve`) and model is pulled.")
else:
    st.info("👈 Please select or upload a document from the sidebar to begin.")
