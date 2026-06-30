# chat — minimal streaming chatbot UI on top of the LLM cluster.
#
# Pure Python web app (no GPU): a slim base is plenty. It only talks HTTP to the
# cluster's HAProxy endpoint, so it can run anywhere on the LAN.
FROM python:3.12-slim

# Deps first so code edits rebuild only the final layer.
COPY requirements.txt /opt/chat/requirements.txt
RUN pip install --no-cache-dir -r /opt/chat/requirements.txt

COPY app/ /opt/chat/app/
WORKDIR /opt/chat

EXPOSE 8800
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
