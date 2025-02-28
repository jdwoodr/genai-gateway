from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, Response
import httpx
import json
from typing import Dict, Any, AsyncGenerator, List, Optional
from openai import AsyncOpenAI
import struct
import zlib
import boto3
import re
import os
import uuid
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    String,
    Text,
    inspect,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import select, insert, update
import hashlib
from okta_jwt_verifier import AccessTokenVerifier
from okta_jwt_verifier.jwt_utils import JWTUtils
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
    expose_headers=["X-Session-Id"],  # Expose the X-Session-Id header
)

LITELLM_ENDPOINT = "http://localhost:4000"
LITELLM_CHAT = f"{LITELLM_ENDPOINT}/v1/chat/completions"

print(f"AWS_REGION: {os.getenv('AWS_REGION')}")
print(f"AWS_DEFAULT_REGION: {os.getenv('AWS_DEFAULT_REGION')}")
bedrock_client = boto3.client("bedrock-agent")

db_engine = None
metadata = MetaData()
chat_sessions = None

OKTA_ISSUER = os.environ.get("OKTA_ISSUER")
OKTA_AUDIENCE = os.environ.get("OKTA_AUDIENCE")
MASTER_KEY = os.environ.get("MASTER_KEY")

# Create a verifier instance for Access Tokens
access_token_verifier = None
if OKTA_AUDIENCE and OKTA_ISSUER:
    access_token_verifier = AccessTokenVerifier(
        issuer=OKTA_ISSUER, audience=OKTA_AUDIENCE
    )
else:
    print(
        f"OKTA_AUDIENCE or OKTA_ISSUER are empty. Support for Okta JWT Auth is disabled."
    )


def setup_database():
    try:
        database_url = os.environ.get("DATABASE_MIDDLEWARE_URL")
        if not database_url:
            raise ValueError("DATABASE_MIDDLEWARE_URL environment variable not set")

        engine = create_engine(database_url)
        metadata_obj = MetaData()

        inspector = inspect(engine)
        if "chat_sessions" not in inspector.get_table_names():
            chat_sessions_table = Table(
                "chat_sessions",
                metadata_obj,
                Column("session_id", String, primary_key=True),
                Column("chat_history", Text),
                Column("api_key_hash", String),  # new column to store hashed API key
            )
            metadata_obj.create_all(engine)
            print("Created chat_sessions table")
        else:
            # If the table exists, ensure it has the api_key_hash column
            chat_sessions_table = Table(
                "chat_sessions", metadata_obj, autoload_with=engine
            )
            columns = [c.name for c in chat_sessions_table.columns]
            if "api_key_hash" not in columns:
                with engine.connect() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE chat_sessions ADD COLUMN api_key_hash VARCHAR;"
                        )
                    )
                    conn.commit()
                # Reload table definition
                chat_sessions_table = Table(
                    "chat_sessions", metadata_obj, autoload_with=engine
                )
            else:
                print("chat_sessions table already exists with api_key_hash column")

        # Check if the index already exists
        indexes = inspector.get_indexes("chat_sessions")
        index_names = [idx["name"] for idx in indexes]

        if "idx_chat_sessions_api_key_hash" not in index_names:
            print(
                "Creating index idx_chat_sessions_api_key_hash on api_key_hash column."
            )
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX idx_chat_sessions_api_key_hash ON chat_sessions (api_key_hash)"
                    )
                )
                conn.commit()
        else:
            print("Index idx_chat_sessions_api_key_hash already exists.")

        return engine, chat_sessions_table
    except SQLAlchemyError as e:
        print(f"Database setup error: {str(e)}")
        raise


@app.on_event("startup")
async def startup_event():
    global db_engine, chat_sessions
    db_engine, chat_sessions = setup_database()


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def get_session_data(session_id: str) -> Optional[Dict[str, Any]]:
    with db_engine.connect() as conn:
        stmt = select(chat_sessions.c.chat_history, chat_sessions.c.api_key_hash).where(
            chat_sessions.c.session_id == session_id
        )
        result = conn.execute(stmt).fetchone()
        if result:
            return {
                "chat_history": json.loads(result[0]) if result[0] else None,
                "api_key_hash": result[1],
            }
    return None


def create_chat_history(
    session_id: str, chat_history: List[Dict[str, str]], api_key_hash: str
):
    with db_engine.connect() as conn:
        stmt = insert(chat_sessions).values(
            session_id=session_id,
            chat_history=json.dumps(chat_history),
            api_key_hash=api_key_hash,
        )
        conn.execute(stmt)
        conn.commit()


def update_chat_history(session_id: str, chat_history: List[Dict[str, str]]):
    with db_engine.connect() as conn:
        stmt = (
            update(chat_sessions)
            .where(chat_sessions.c.session_id == session_id)
            .values(chat_history=json.dumps(chat_history))
        )
        conn.execute(stmt)
        conn.commit()


class CustomEventStream:
    def __init__(self, messages):
        self.messages = messages
        self.position = 0

    def stream(self):
        while self.position < len(self.messages):
            yield self.messages[self.position]
            self.position += 1


def create_event_message(payload, event_type_name):
    header_name = b":event-type"
    header_name_length = len(header_name)
    event_name_bytes = event_type_name.encode("utf-8")
    event_name_length = len(event_name_bytes)

    headers_bytes = (
        struct.pack("B", header_name_length)
        + header_name
        + b"\x07"
        + struct.pack(">H", event_name_length)
        + event_name_bytes
    )

    headers_length = len(headers_bytes)
    payload_length = len(payload)
    total_length = payload_length + headers_length + 16

    prelude = struct.pack(">I", total_length) + struct.pack(">I", headers_length)
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)

    message_parts = prelude + prelude_crc + headers_bytes + payload
    message_crc = struct.pack(">I", zlib.crc32(message_parts) & 0xFFFFFFFF)

    return message_parts + message_crc


def convert_messages_to_openai(
    bedrock_messages: List[Dict[str, Any]],
    system: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    openai_messages = []

    if system:
        system_text = " ".join(item.get("text", "") for item in system)
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

    for msg in bedrock_messages:
        role = msg.get("role")
        content = ""
        if "content" in msg:
            for content_item in msg["content"]:
                if "text" in content_item:
                    content += content_item["text"]

        openai_messages.append({"role": role, "content": content})

    return openai_messages


async def convert_bedrock_to_openai(
    model_id: str, bedrock_request: Dict[str, Any], streaming: bool
) -> Dict[str, Any]:
    prompt_variables = bedrock_request.get("promptVariables", {})
    final_prompt_text = None
    if model_id.startswith("arn:aws:bedrock:"):
        prompt_id, prompt_version = parse_prompt_arn(model_id)
        if prompt_id:
            if prompt_version:
                prompt = bedrock_client.get_prompt(
                    promptIdentifier=prompt_id, promptVersion=prompt_version
                )
            else:
                prompt = bedrock_client.get_prompt(promptIdentifier=prompt_id)

            variants = prompt.get("variants", [])
            variant = variants[0]
            template_text = variant["templateConfiguration"]["text"]["text"]

            validate_prompt_variables(template_text, prompt_variables)
            final_prompt_text = construct_prompt_text_from_variables(
                template_text, prompt_variables
            )
            model_id = variant["modelId"]

    completion_params = {"model": model_id}

    if final_prompt_text:
        final_prompt_messages = [
            {"role": "user", "content": [{"text": final_prompt_text}]}
        ]
        messages = convert_messages_to_openai(final_prompt_messages, [])
    else:
        messages = convert_messages_to_openai(
            bedrock_request.get("messages", []), bedrock_request.get("system", [])
        )

    completion_params["messages"] = messages
    if streaming:
        completion_params["stream"] = True

    if "inferenceConfig" in bedrock_request:
        config = bedrock_request["inferenceConfig"]
        if "temperature" in config:
            completion_params["temperature"] = config["temperature"]
        if "maxTokens" in config:
            completion_params["max_tokens"] = config["maxTokens"]
        if "stopSequences" in config:
            completion_params["stop"] = config["stopSequences"]
        if "topP" in config:
            completion_params["top_p"] = config["topP"]

    if "additionalModelRequestFields" in bedrock_request:
        # Exclude "session_id" from being added to completion_params
        additional_fields = {
            key: value
            for key, value in bedrock_request["additionalModelRequestFields"].items()
            if key != "session_id"
        }
        completion_params.update(additional_fields)

    return completion_params


async def convert_openai_to_bedrock(openai_response: Dict[str, Any]) -> Dict[str, Any]:
    bedrock_response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": openai_response["choices"][0]["message"]["content"]}
                ],
            }
        },
        "usage": {
            "inputTokens": openai_response["usage"]["prompt_tokens"],
            "outputTokens": openai_response["usage"]["completion_tokens"],
            "totalTokens": openai_response["usage"]["total_tokens"],
        },
    }

    if "finish_reason" in openai_response["choices"][0]:
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "content_filtered",
        }
        finish_reason = openai_response["choices"][0]["finish_reason"]
        bedrock_response["stopReason"] = stop_reason_map.get(finish_reason, "end_turn")

    return bedrock_response


async def openai_stream_to_bedrock_chunks(openai_stream):
    async for chunk in openai_stream:
        delta = chunk.choices[0].delta
        finish_reason = chunk.choices[0].finish_reason

        if delta.role:
            event_payload = json.dumps({"role": delta.role}).encode("utf-8")
            yield create_event_message(event_payload, "messageStart")

        if delta.content:
            event_payload = json.dumps(
                {
                    "contentBlockIndex": 0,
                    "delta": {"text": delta.content},
                }
            ).encode("utf-8")
            yield create_event_message(event_payload, "contentBlockDelta")

        if finish_reason == "stop":
            event_payload = json.dumps({"stopReason": "end_turn"}).encode("utf-8")
            yield create_event_message(event_payload, "messageStop")


def parse_prompt_arn(arn: str):
    if "prompt/" not in arn:
        return None, None

    after_prompt = arn.split("prompt/", 1)[1]

    if ":" in after_prompt:
        prompt_id, prompt_version = after_prompt.split(":", 1)
        return prompt_id, prompt_version
    else:
        return after_prompt, None


def validate_prompt_variables(template_text: str, variables: Dict[str, Any]):
    found_placeholders = re.findall(r"{{\s*(\w+)\s*}}", template_text)
    placeholders_set = set(found_placeholders)
    variables_set = set(variables.keys())

    if placeholders_set != variables_set:
        detail_message = {
            "message": f"Prompt variable mismatch. Template placeholders: {placeholders_set}. Provided variables: {variables_set}."
        }
        raise HTTPException(status_code=400, detail=detail_message)


def construct_prompt_text_from_variables(template_text: str, variables: dict) -> str:
    for var_name, var_value in variables.items():
        value = var_value.get("text", "")
        template_text = template_text.replace(f"{{{{{var_name}}}}}", value)
    return template_text


@app.get("/")
@app.get("/bedrock/health/liveliness")
async def health_check():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{LITELLM_ENDPOINT}/health/liveliness", timeout=5.0
            )
            if response.status_code == 200:
                return JSONResponse(
                    content={"status": "healthy", "litellm": "connected"}
                )
            else:
                return JSONResponse(
                    status_code=503, content={"status": "unhealthy", "litellm": "error"}
                )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "litellm": "disconnected", "error": str(e)},
        )


async def process_chat_request(
    model_id: str, request: Request
) -> (Dict[str, Any], str):
    body = await request.json()
    session_id = body.get("additionalModelRequestFields", {}).get("session_id", None)
    print(f"session_id: {session_id}")
    auth_header = request.headers.get("Authorization")
    print(f"auth_header: {auth_header}")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        print(f"Missing or invalid Authorization header: {auth_header}")
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )

    provided_hash = hash_api_key(api_key)
    print(f"provided_hash: {provided_hash}")

    if session_id is not None:
        session_data = get_session_data(session_id)
        print(f"session_data: {session_data}")
        if session_data is not None:
            # Verify API key hash matches
            if session_data["api_key_hash"] != provided_hash:
                print(
                    f"Unauthorized: API key does not match session owner: {session_data['api_key_hash']} provided_hash: {provided_hash}"
                )
                raise HTTPException(
                    status_code=401,
                    detail="Unauthorized: API key does not match session owner",
                )
            chat_history = (
                session_data["chat_history"] if session_data["chat_history"] else []
            )
            print(f"chat_history: {chat_history}")

        else:
            print(f"creating chat history and session_id is not None")
            chat_history = []
            create_chat_history(session_id, chat_history, provided_hash)
    else:
        print(f"creating chat history and session_id is None")
        session_id = str(uuid.uuid4())
        chat_history = []
        create_chat_history(session_id, chat_history, provided_hash)

    openai_format = await convert_bedrock_to_openai(model_id, body, False)
    print(f"openai_format: {openai_format}")

    # Append the last user message to chat_history
    user_messages_this_round = [
        m for m in openai_format["messages"] if m["role"] == "user"
    ]
    if user_messages_this_round:
        chat_history.append(user_messages_this_round[-1])

    # Replace openai_format["messages"] with the full chat_history
    openai_format["messages"] = chat_history

    async with httpx.AsyncClient() as client:
        response = await client.post(
            LITELLM_CHAT,
            json=openai_format,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": f"Error from LiteLLM endpoint: {response.text}"},
            )

        openai_response = response.json()
        bedrock_response = await convert_openai_to_bedrock(openai_response)

    # Append assistant's response to history
    assistant_message = openai_response["choices"][0]["message"]
    chat_history.append({"role": "assistant", "content": assistant_message["content"]})
    update_chat_history(session_id, chat_history)

    bedrock_response["session_id"] = session_id
    return bedrock_response, session_id


async def process_streaming_chat_request(
    model_id: str, request: Request
) -> (AsyncGenerator, str, List[Dict[str, str]], List[str]):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )

    provided_hash = hash_api_key(api_key)
    session_id = body.get("additionalModelRequestFields", {}).get("session_id", None)

    if session_id is not None:
        session_data = get_session_data(session_id)
        if session_data is not None:
            if session_data["api_key_hash"] != provided_hash:
                raise HTTPException(
                    status_code=401,
                    detail="Unauthorized: API key does not match session owner",
                )
            chat_history = (
                session_data["chat_history"] if session_data["chat_history"] else []
            )
        else:
            chat_history = []
            create_chat_history(session_id, chat_history, provided_hash)
    else:
        session_id = str(uuid.uuid4())
        chat_history = []
        create_chat_history(session_id, chat_history, provided_hash)

    openai_params = await convert_bedrock_to_openai(model_id, body, True)

    # Append the user message to chat_history
    user_messages_this_round = [
        m for m in openai_params["messages"] if m["role"] == "user"
    ]
    if user_messages_this_round:
        chat_history.append(user_messages_this_round[-1])

    openai_params["messages"] = chat_history

    print(f'final message sent to llm: {openai_params["messages"]}')

    client = AsyncOpenAI(api_key=api_key, base_url=LITELLM_ENDPOINT)
    stream = await client.chat.completions.create(**openai_params)

    assistant_content_parts = []

    async def stream_wrapper():
        message_started = False
        content_block_index = 0
        async for chunk in stream:
            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason

            if delta.role and not message_started:
                event_payload = json.dumps({"role": delta.role}).encode("utf-8")
                yield create_event_message(event_payload, "messageStart")
                message_started = True

            if delta.content:
                assistant_content_parts.append(delta.content)
                event_payload = json.dumps(
                    {
                        "contentBlockIndex": content_block_index,
                        "delta": {"text": delta.content},
                    }
                ).encode("utf-8")
                yield create_event_message(event_payload, "contentBlockDelta")

            if finish_reason == "stop":
                event_payload = json.dumps({"stopReason": "end_turn"}).encode("utf-8")
                yield create_event_message(event_payload, "messageStop")

    return stream_wrapper(), session_id, chat_history, assistant_content_parts


async def finalize_streaming_chat_history(
    session_id: str,
    chat_history: List[Dict[str, str]],
    assistant_content_parts: List[str],
):
    assistant_message = {
        "role": "assistant",
        "content": "".join(assistant_content_parts),
    }
    chat_history.append(assistant_message)
    update_chat_history(session_id, chat_history)


@app.post("/bedrock/model/{prompt_arn_prefix}/{prompt_id}/converse-stream")
async def handle_bedrock_streaming_request_prompts(
    prompt_arn_prefix: str, prompt_id: str, request: Request
):
    full_arn = prompt_arn_prefix + "/" + prompt_id
    return await handle_bedrock_streaming_request(full_arn, request)


@app.post("/bedrock/model/{model_id}/converse-stream")
async def handle_bedrock_streaming_request(model_id: str, request: Request):
    try:
        stream_wrapper, session_id, chat_history, assistant_content_parts = (
            await process_streaming_chat_request(model_id, request)
        )

        async def finalizing_stream():
            async for event in stream_wrapper:
                yield event
            await finalize_streaming_chat_history(
                session_id, chat_history, assistant_content_parts
            )

        response = StreamingResponse(
            finalizing_stream(), media_type="application/vnd.amazon.eventstream"
        )
        response.headers["X-Session-Id"] = session_id
        return response
    except HTTPException as he:
        return JSONResponse(
            status_code=400,
            content={
                "Message": he.detail,
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "Message": f"Internal server error: {str(e)}",
            },
        )


@app.post("/bedrock/model/{prompt_arn_prefix}/{prompt_id}/converse")
async def handle_bedrock_request_prompts(
    prompt_arn_prefix: str, prompt_id: str, request: Request
):
    full_arn = prompt_arn_prefix + "/" + prompt_id
    return await handle_bedrock_request(full_arn, request)


@app.post("/bedrock/model/{model_id}/converse")
async def handle_bedrock_request(model_id: str, request: Request):
    try:
        bedrock_response, session_id = await process_chat_request(model_id, request)
        return JSONResponse(
            content=bedrock_response, headers={"X-Session-Id": session_id}
        )
    except HTTPException as he:
        print(f"HTTPException he: {he}")
        return JSONResponse(
            status_code=he.status_code,
            content={
                "Message": he.detail,
            },
        )
    except Exception as e:
        print(f"exception e: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "Message": f"Internal server error: {str(e)}",
            },
        )


async def forward_openai_stream(stream) -> AsyncGenerator[bytes, None]:
    """
    Forward the streaming response from OpenAI client.
    """
    try:
        async for chunk in stream:
            # Convert the chunk to the same format as the API response
            yield f"data: {json.dumps(chunk.model_dump())}\n\n".encode("utf-8")
    except Exception as e:
        print(f"Streaming error: {e}")
        raise


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def proxy_request(request: Request):
    body = await request.body()

    try:
        data = json.loads(body)
        is_streaming = data.get("stream", False)

        session_id = data.pop("session_id", None)

        # Get API key from headers
        api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail={"error": "Missing or invalid Authorization header"},
            )
        provided_hash = hash_api_key(api_key)

        if session_id is not None:
            session_data = get_session_data(session_id)
            if session_data is not None:
                if session_data["api_key_hash"] != provided_hash:
                    raise HTTPException(
                        status_code=401,
                        detail={
                            "error": "Unauthorized: API key does not match session owner"
                        },
                    )
                chat_history = (
                    session_data["chat_history"] if session_data["chat_history"] else []
                )
            else:
                chat_history = []
                create_chat_history(session_id, chat_history, provided_hash)
        else:
            session_id = str(uuid.uuid4())
            chat_history = []
            create_chat_history(session_id, chat_history, provided_hash)

        # Merge incoming user messages into chat history
        user_messages_this_round = [
            m for m in data.get("messages", []) if m["role"] == "user"
        ]
        if user_messages_this_round:
            chat_history.append(user_messages_this_round[-1])

        data["messages"] = chat_history

        # Check for prompt ARN logic
        model_id = data.get("model")
        prompt_variables = data.pop("promptVariables", {})
        final_prompt_text = None
        if model_id and model_id.startswith("arn:aws:bedrock:"):
            prompt_id, prompt_version = parse_prompt_arn(model_id)
            if prompt_id:
                if prompt_version:
                    prompt = bedrock_client.get_prompt(
                        promptIdentifier=prompt_id, promptVersion=prompt_version
                    )
                else:
                    prompt = bedrock_client.get_prompt(promptIdentifier=prompt_id)

                variants = prompt.get("variants", [])
                variant = variants[0]
                template_text = variant["templateConfiguration"]["text"]["text"]

                validate_prompt_variables(template_text, prompt_variables)
                final_prompt_text = construct_prompt_text_from_variables(
                    template_text, prompt_variables
                )

                if "modelId" in variant:
                    data["model"] = variant["modelId"]

        if final_prompt_text:
            data["messages"] = [{"role": "user", "content": final_prompt_text}]

        client = AsyncOpenAI(api_key=api_key, base_url=LITELLM_ENDPOINT)

        if is_streaming:
            stream = await client.chat.completions.create(**data)

            assistant_content_parts = []

            async def stream_wrapper():
                first_chunk = True
                async for chunk in stream:
                    chunk_dict = chunk.model_dump()
                    if first_chunk:
                        chunk_dict["session_id"] = session_id
                        first_chunk = False
                    yield f"data: {json.dumps(chunk_dict)}\n\n".encode("utf-8")

                    choice = chunk_dict["choices"][0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason")
                    if "content" in delta and delta["content"]:
                        assistant_content_parts.append(delta["content"])

                    if finish_reason == "stop":
                        break

            async def finalizing_stream():
                async for event in stream_wrapper():
                    yield event

                if assistant_content_parts:
                    assistant_message = {
                        "role": "assistant",
                        "content": "".join(assistant_content_parts),
                    }
                    chat_history.append(assistant_message)
                    update_chat_history(session_id, chat_history)

            response = StreamingResponse(
                finalizing_stream(), media_type="text/event-stream"
            )
            return response

        else:
            response = await client.chat.completions.create(**data)
            response_dict = response.model_dump()

            if response_dict.get("choices"):
                assistant_message = response_dict["choices"][0]["message"]
                chat_history.append(
                    {"role": "assistant", "content": assistant_message["content"]}
                )
                update_chat_history(session_id, chat_history)

            response_dict["session_id"] = session_id
            return Response(
                content=json.dumps(response_dict),
                media_type="application/json",
            )

    except json.JSONDecodeError:
        return Response(
            content=json.dumps({"error": "Invalid JSON"}),
            status_code=400,
            media_type="application/json",
        )
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content=he.detail)
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=500,
            media_type="application/json",
        )


def convert_openai_to_bedrock_history(
    openai_history: List[Dict[str, str]]
) -> Dict[str, Any]:
    system_messages = []
    bedrock_messages = []

    for msg in openai_history:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            system_messages.append({"text": content})
        else:
            bedrock_messages.append({"role": role, "content": [{"text": content}]})

    return {"messages": bedrock_messages, "system": system_messages}


@app.post("/bedrock/chat-history")
async def get_bedrock_chat_history(request: Request):
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    # We must verify the API key for this history as well
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )
    provided_hash = hash_api_key(api_key)

    session_data = get_session_data(session_id)
    if not session_data or session_data["api_key_hash"] != provided_hash:
        raise HTTPException(
            status_code=401,
            detail={"error": "Unauthorized: API key does not match session owner"},
        )

    chat_history = session_data["chat_history"]
    if chat_history is None:
        return {"messages": [], "system": []}
    bedrock_format = convert_openai_to_bedrock_history(chat_history)
    return bedrock_format


@app.post("/chat-history")
async def get_openai_chat_history(request: Request):
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    # Verify the API key
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )
    provided_hash = hash_api_key(api_key)

    session_data = get_session_data(session_id)
    if not session_data or session_data["api_key_hash"] != provided_hash:
        raise HTTPException(
            status_code=401,
            detail={"error": "Unauthorized: API key does not match session owner"},
        )

    chat_history = session_data["chat_history"]
    if chat_history is None:
        chat_history = []
    return {"messages": chat_history}


@app.post("/session-ids")
async def list_session_ids_for_api_key(request: Request):
    # Verify the API key
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )

    provided_hash = hash_api_key(api_key)

    # Query all session_ids for this api_key_hash
    with db_engine.connect() as conn:
        stmt = select(chat_sessions.c.session_id).where(
            chat_sessions.c.api_key_hash == provided_hash
        )
        results = conn.execute(stmt).fetchall()

    session_ids = [row[0] for row in results]

    return {"session_ids": session_ids}


# ToDo: Enforce that a non-admin user can only create keys for themself if this bug isn't fixed in a timely manner https://github.com/BerriAI/litellm/issues/7336
@app.post("/key/generate")
async def forward_key_generate(request: Request):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{LITELLM_ENDPOINT}/key/generate",
            content=await request.body(),
            headers=request.headers,
        )
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=response.headers,
        )


@app.post("/user/new")
async def forward_user_new(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )

    token = auth_header[len("Bearer ") :]
    final_headers = dict(request.headers)
    request_body = await request.body()
    body_json = json.loads(request_body)

    if not token.startswith("sk-") and access_token_verifier:
        print(f"token is not api key, assume it is JWT")
        # Handle as JWT
        try:
            await access_token_verifier.verify(token)
            print(f"token is verified.")
        except Exception as e:
            print(f"exception: {e}")
            # If the JWT verification fails, user is not authorized.
            raise HTTPException(
                status_code=401, detail={"error": "Invalid or expired token"}
            ) from e

        headers, claims, signing_input, signature = JWTUtils.parse_token(token)
        print(
            f"headers: {headers} claims: {claims} signing_input: {signing_input} signature: {signature}"
        )

        sub = claims.get("sub")
        print(f"sub: {sub}")
        if not sub:
            raise HTTPException(
                status_code=403, detail={"error": "No sub claim found in the token"}
            )

        # For random Okta users, we want to bind their okta sub/id to their litellm user_id. So that the relationship between okta users and litellm users is 1:1
        # We also want random Okta users to not be able to make themselves admins, so we lock their user_role to "internal_user"
        # Only someone with the master key (or users/keys derived from the master key) will be able to perform any admin operations
        # At a later point, we can decide that someone with a specific Okta claim is able to act as an admin and bypass these restrictions without needing the master key
        # Right now, users can give themselves any max_budget, tpm_limit, rpm_limit, max_parallel_requests, or teams. At a later point, we can lock these down more, or make a default configurable in the deployment.
        body_json["user_email"] = sub
        body_json["user_id"] = sub
        body_json["user_role"] = "internal_user"
        print(f"body_json: {body_json}")
        request_body = json.dumps(body_json).encode()
        final_headers["content-length"] = str(len(request_body))
        final_headers["authorization"] = f"Bearer {MASTER_KEY}"

    print(f"final_headers: {final_headers}")
    print(f"request_body: {request_body}")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{LITELLM_ENDPOINT}/user/new",
            content=request_body,
            headers=final_headers,
        )
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=response.headers,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
