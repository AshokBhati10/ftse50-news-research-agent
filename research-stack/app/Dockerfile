FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models

WORKDIR /app

# CPU-only PyTorch (≈200 MB instead of several GB of CUDA wheels).
# For a GPU box, use the matching CUDA index URL and set MODEL_DEVICE=cuda.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY research_agent ./research_agent

EXPOSE 8000
CMD ["uvicorn", "research_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
