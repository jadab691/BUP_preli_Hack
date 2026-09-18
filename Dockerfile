FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
ENV LLM_MODEL=gemini-3.5-flash

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]