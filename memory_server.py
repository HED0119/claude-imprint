import os
import json
import secrets
import hashlib
import base64
import psycopg2
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Header, HTTPException, Form
from fastapi.responses import JSONResponse, RedirectResponse

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL")
API_KEY = os.environ.get("API_KEY", secrets.token_hex(32))
CLIENT_ID = os.environ.get("CLIENT_ID", "memory-client")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "memory-secret")

auth_codes = {}
tokens = {}

def get_db():
    return psycopg2.connect(DATABASE_URL)

def ensure_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            tags TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def tool_save_memory(content, tags=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO memories (content, tags) VALUES (%s, %s) RETURNING id", (content, tags))
    mid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return f"已保存记忆 #{mid}"

def tool_search_memory(query):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, content, tags, created_at FROM memories WHERE content ILIKE %s ORDER BY created_at DESC LIMIT 10",
        (f"%{query}%",)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return "没有找到相关记忆"
    return "\n".join([f"[{r[3].strftime('%Y-%m-%d %H:%M')}] #{r[0]}: {r[1]}" for r in rows])

def tool_get_memories(limit=20):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return "还没有保存任何记忆"
    return "\n".join([f"[{r[3].strftime('%Y-%m-%d %H:%M')}] #{r[0]}: {r[1]}" for r in rows])

def tool_delete_memory(memory_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM memories WHERE id = %s RETURNING id", (memory_id,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return f"已删除记忆 #{memory_id}" if row else f"未找到记忆 #{memory_id}"

TOOLS = [
    {
        "name": "save_memory",
        "description": "保存一条记忆到数据库",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "tags": {"type": "string", "description": "标签，逗号分隔（可选）"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "search_memory",
        "description": "按关键词搜索记忆",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"]
        }
    },
    {
        "name": "get_memories",
        "description": "获取最近的记忆列表",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "返回数量，默认20"}}
        }
    },
    {
        "name": "delete_memory",
        "description": "删除指定ID的记忆",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "integer", "description": "记忆ID"}},
            "required": ["memory_id"]
        }
    }
]

@app.get("/authorize")
async def oauth_authorize(client_id: str, redirect_uri: str, response_type: str, state: str = "", code_challenge: str = "", code_challenge_method: str = ""):
    code = secrets.token_urlsafe(32)
    auth_codes[code] = {"redirect_uri": redirect_uri, "code_challenge": code_challenge}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}")

@app.post("/token")
async def oauth_token(grant_type: str = Form(...), code: str = Form(None), redirect_uri: str = Form(None), client_id: str = Form(None), client_secret: str = Form(None), code_verifier: str = Form(None)):
    if grant_type == "authorization_code":
        if code not in auth_codes:
            raise HTTPException(400, "invalid_grant")
        auth_codes.pop(code)
        token = secrets.token_urlsafe(32)
        tokens[token] = True
        return {"access_token": token, "token_type": "bearer", "expires_in": 86400 * 365}
    raise HTTPException(400, "unsupported_grant_type")

def verify_token(authorization: str = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = authorization[7:]
    if token not in tokens and token != API_KEY:
        raise HTTPException(401, "Unauthorized")

@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: str = Header(None)):
    verify_token(authorization)
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "memory-server", "version": "1.0.0"}}})
    elif method in ("notifications/initialized", "ping"):
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        try:
            if name == "save_memory":
                result = tool_save_memory(args["content"], args.get("tags"))
            elif name == "search_memory":
                result = tool_search_memory(args["query"])
            elif name == "get_memories":
                result = tool_get_memories(args.get("limit", 20))
            elif name == "delete_memory":
                result = tool_delete_memory(args["memory_id"])
            else:
                result = f"未知工具: {name}"
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": result}]}})
        except Exception as e:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}})

    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.on_event("startup")
async def startup():
    ensure_table()
    print(f"Memory server started")
    print(f"API_KEY: {API_KEY}")
