from flask import Blueprint, request, Response, stream_with_context, jsonify
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.core.settings import Settings
from query import ask_archivist, stream_archivist_response, get_system_prompt
from utils.index_utils import wait_for_ollama, load_or_create_index
from config import VECTORSTORE_DIR, LORE_PATH, MODEL_NAME, OLLAMA_API_BASE_URL

import os
import logging
import re
import json
import uuid

openai_bp = Blueprint("openai_compatible", __name__)

# Embedding model setup
Settings.embed_model = OllamaEmbedding(model_name=MODEL_NAME, base_url=OLLAMA_API_BASE_URL)
wait_for_ollama()

# Load the lore index
index = load_or_create_index()
if index:
    logging.info("openai_compatible: Using index from unified loader.")
else:
    logging.error("openai_compatible: Lore index is not available!")

def parse_answer(answer_text):
    """Parses the raw answer text into a title and answer body."""
    pattern = r"Title:\s*(.*?)\s*Answer:\s*(.*)"
    match = re.search(pattern, answer_text, re.DOTALL | re.IGNORECASE)
    if match:
        title = match.group(1).strip()
        content = match.group(2).strip()
    else:
        parts = answer_text.splitlines()
        if len(parts) >= 2 and len(parts[0].split()) <= 10:
            title = parts[0].strip()
            content = "\n".join(parts[1:]).strip()
        else:
            title = ""
            content = answer_text.strip()
    return title, content

@openai_bp.route("/v1/chat/completions", methods=["POST"])
def completions():
    if index is None:
        return jsonify({"error": "Lore index is not available."}), 500

    data = request.get_json()
    messages = data.get("messages", [])
    stream = data.get("stream", False)

    model_id = "elesh-archivist"
    completion_id = str(uuid.uuid4())

    if stream:
        def event_stream():
            try:
                for chunk in stream_archivist_response(messages, index):
                    try:
                        chunk_json = json.loads(chunk)
                        content = chunk_json.get("response", "")
                        done = chunk_json.get("done", False)

                        if content:
                            yield f"data: {json.dumps({ 'id': completion_id, 'object': 'chat.completion.chunk', 'choices': [{ 'delta': {'content': content}, 'index': 0, 'finish_reason': None }], 'model': model_id })}\n\n"

                        if done:
                            yield f"data: {json.dumps({ 'id': completion_id, 'object': 'chat.completion.chunk', 'choices': [{ 'delta': {}, 'index': 0, 'finish_reason': 'stop' }], 'model': model_id })}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                    except Exception:
                        logging.exception("Error while parsing streamed chunk.")
                        break
            except Exception:
                logging.exception("Streaming failed.")
                yield "data: [ERROR] Streaming failure\n\n"

        return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

    # Non-streamed fallback
    raw_answer = ask_archivist(messages, index)
    title, content = parse_answer(raw_answer)

    return jsonify({
        "id": completion_id,
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "title": title,
                "content": content
            },
            "finish_reason": "stop"
        }],
        "model": model_id,
        "system_prompt": get_system_prompt()
    })

@openai_bp.route("/v1/models", methods=["GET"])
def models():
    return jsonify({
        "object": "list",
        "data": [{
            "id": "elesh-archivist",
            "object": "model",
            "created": 0,
            "owned_by": "user",
            "permission": []
        }]
    })