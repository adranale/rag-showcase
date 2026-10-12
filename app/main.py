from fastapi import FastAPI

app = FastAPI(
    title="RAG Showcase",
    description="Document assistant for Google Drive and Dropbox",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {"status": "ok"}
