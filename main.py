from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Text, DateTime, Integer
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.declarative import declarative_base
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_groq import ChatGroq
from dotenv import load_dotenv
from typing import List, Optional
from collections import defaultdict
import os, shutil, re, httpx, json, random, time

load_dotenv()

DATABASE_URL = "sqlite:///./sonic_ai.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

SECRET_KEY = os.getenv("SECRET_KEY", "sonic-ai-secret-2024-xk9mq")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

pwd_context   = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

_rate_store: dict = defaultdict(list)
RATE_LIMIT = 40

def check_rate(ip: str) -> bool:
    now = time.time()
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        return False
    _rate_store[ip].append(now)
    return True


class UserDB(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String, unique=True, index=True)
    username        = Column(String)
    hashed_password = Column(String)
    avatar          = Column(String, default="🧑")
    created_at      = Column(DateTime, default=datetime.utcnow)

class ChatHistoryDB(Base):
    __tablename__ = "chat_history"
    id            = Column(Integer, primary_key=True, index=True)
    user_email    = Column(String, index=True)
    session_title = Column(String, default="New Chat")
    messages      = Column(Text, default="[]")
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow)
    
class FailedLoginDB(Base):
    __tablename__ = "failed_logins"
    id        = Column(Integer, primary_key=True, index=True)
    email     = Column(String, index=True)
    ip        = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow)    

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Sonic AI", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
llm        = ChatGroq(api_key=os.getenv("GROQ_API_KEY"), model_name="llama-3.3-70b-versatile")
db_vector  = None

def load_db():
    global db_vector
    if os.path.exists("vectorstore"):
        try:
            db_vector = FAISS.load_local(
                "vectorstore", embeddings, allow_dangerous_deserialization=True
            )
        except Exception:
            db_vector = None

load_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception as e:
        print(f"[verify_password error] {e}")
        return False

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict) -> str:
    d = data.copy()
    d["exp"] = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode(d, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email   = payload.get("sub")
        if not email:
            return None
        return db.query(UserDB).filter(UserDB.email == email).first()
    except JWTError:
        return None

class UserCreate(BaseModel):
    email: str
    username: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class AvatarUpdate(BaseModel):
    avatar: str

class Message(BaseModel):
    role: str
    content: str

class QuestionRequest(BaseModel):
    question:        str
    history:         Optional[List[Message]] = []
    format_hint:     Optional[str]  = "auto"
    language:        Optional[str]  = "english"
    mode:            Optional[str]  = "effort"
    humanize:        Optional[bool] = False
    doc_mode:        Optional[str]  = "docweb"
    removed_context: Optional[str]  = ""

class SaveHistoryRequest(BaseModel):
    session_id:    Optional[int]  = None
    session_title: Optional[str]  = "New Chat"
    messages:      List[dict]

class ImprovePromptRequest(BaseModel):
    prompt: str

SKIP_WORDS = {
    "hi","hello","hey","ok","okay","thanks","bye","sup","yo","hiya",
    "yes","no","sure","great","good","nice","what","how","why"
}

def smart_title(messages: list) -> str:
    best = ""
    for m in messages:
        if m.get("role") == "user":
            c     = m.get("content", "").strip()
            words = c.lower().split()
            if len(words) <= 2 and all(w.rstrip("!?.,'") in SKIP_WORDS for w in words):
                continue
            if len(c) > len(best):
                best = c
    if not best:
        for m in messages:
            if m.get("role") == "user":
                return m.get("content", "New Chat")[:50]
        return "New Chat"
    return best[:50]

def detect_format(question: str, hint: str) -> str:
    if hint != "auto":
        return hint
    q = question.lower().strip()

    if any(k in q for k in ["write code","implement","def ","class ","algorithm",
                              "script","build a","debug","fix code","show code","```"]):
        return "code"
    if any(k in q for k in ["compare","difference between","vs ","versus",
                              "pros and cons","advantages and disadvantages","side by side"]):
        return "table"
    if any(k in q for k in ["detail","detailed","explain in detail","elaborate",
                              "in depth","comprehensive","thorough","full explanation","describe in detail"]):
        return "paragraph"
    if any(k in q for k in ["how to ","steps to","step by step","procedure",
                              "guide","tutorial","walkthrough","how do i ","how can i "]):
        return "numbered"
    if any(k in q for k in ["list ","what are ","give me ","types of","examples of",
                              "features of","benefits","reasons","ways to","mention","name some","explain","describe"]):
        return "bullets"
    if any(k in q for k in ["what is ","who is ","when ","where ","define ",
                              "meaning","brief","quick","capital of","age of"]):
        return "short"
    return "short"

def fmt_instr(fmt: str, lang: str, human: bool, mode: str) -> str:
    l = f" Respond in {lang}." if lang.lower() != "english" else ""
    h = (" Write naturally like a knowledgeable friend — contractions, real examples, "
         "never say 'certainly', 'absolutely', 'of course'.") if human else ""
    detail = mode != "fast"

    return {
        "paragraph": f"Write a thorough, well-structured answer in paragraphs. "
                     f"{'Comprehensive with examples.' if detail else '2 paragraphs max.'}{l}{h}",
        "bullets":   f"Use bullet points (•). {'5-8 detailed points.' if detail else '3-5 concise.'}{l}{h}",
        "table":     f"Use a markdown table with bold headers.{l}{h}",
        "numbered":  f"Use numbered steps. {'Explain each.' if detail else 'Brief steps.'}{l}{h}",
        "code":      f"Complete working code in ``` blocks with language tag and comments.{l}",
        "short":     f"Answer in {'2-4 sentences' if detail else '1-2 sentences'}. Direct, no padding.{l}{h}",
    }.get(fmt, f"Answer directly.{l}{h}")

UNSAFE = ["porn","sex","nude","naked","18+","xxx","erotic","explicit","nsfw"]
GREETINGS = {
    "hi","hello","hey","how are you","good morning","good afternoon","good evening",
    "ok","okay","thanks","thank you","bye","sup","yo","wassup","hiya"
}
GREET_R = [
    "Hey! ⚡ What can I help you with?",
    "Hello! What's on your mind?",
    "Hi! Ask me anything.",
    "Hey! ⚡ Fire away.",
    "Hello! How can I help today?",
]

def is_unsafe(t: str) -> bool:
    return any(w in t.lower() for w in UNSAFE)

@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    email    = user.email.lower().strip()
    username = user.username.strip()
    password = user.password

    if not email or "@" not in email or "." not in email:
        raise HTTPException(400, "Please enter a valid email address.")
    if len(username) < 2:
        raise HTTPException(400, "Username must be at least 2 characters.")
    if len(username) > 50:
        raise HTTPException(400, "Username too long (max 50 chars).")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if len(password) > 256:
        raise HTTPException(400, "Password too long (max 256 chars).")

    existing = db.query(UserDB).filter(UserDB.email == email).first()
    if existing:
        raise HTTPException(400, "This email is already registered. Please sign in.")

    try:
        hashed = get_password_hash(password)
        new_user = UserDB(
            email=email,
            username=username,
            hashed_password=hashed,
            avatar="🧑"
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        token = create_access_token({"sub": new_user.email})
        return {
            "token":    token,
            "username": new_user.username,
            "email":    new_user.email,
            "avatar":   new_user.avatar
        }
    except Exception as e:
        db.rollback()
        print(f"[register error] {e}")
        raise HTTPException(500, f"Registration error: {str(e)}")

@app.post("/login")
def login(user: UserLogin, request: Request, db: Session = Depends(get_db)):
    if not user.email or not user.password:
        raise HTTPException(400, "Please fill all fields.")

    email   = user.email.lower().strip()
    db_user = db.query(UserDB).filter(UserDB.email == email).first()
    ip      = request.client.host or "unknown"

    if not db_user:
        db.add(FailedLoginDB(email=email, ip=ip))
        db.commit()
        raise HTTPException(401, "No account found with this email.")

    if not verify_password(user.password, db_user.hashed_password):
        db.add(FailedLoginDB(email=email, ip=ip))
        db.commit()
        raise HTTPException(401, "Wrong password. Please try again.")

    try:
        token = create_access_token({"sub": db_user.email})
        return {
            "token":    token,
            "username": db_user.username,
            "email":    db_user.email,
            "avatar":   db_user.avatar or "🧑"
        }
    except Exception as e:
        print(f"[login error] {e}")
        raise HTTPException(500, f"Login error: {str(e)}")

@app.post("/update-avatar")
def update_avatar(req: AvatarUpdate, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user:
        raise HTTPException(401, "Login required")
    current_user.avatar = req.avatar
    db.commit()
    return {"avatar": req.avatar}

@app.get("/me")
def get_me(current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(401, "Not authenticated")
    return {
        "username": current_user.username,
        "email":    current_user.email,
        "avatar":   current_user.avatar or "🧑"
    }

@app.post("/history/save")
def save_history(
    req: SaveHistoryRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user:
        return {"error": "Login required"}
    try:
        title = smart_title(req.messages)
        if req.session_id:
            s = db.query(ChatHistoryDB).filter(
                ChatHistoryDB.id == req.session_id,
                ChatHistoryDB.user_email == current_user.email
            ).first()
            if s:
                s.messages      = json.dumps(req.messages)
                s.session_title = title
                s.updated_at    = datetime.utcnow()
                db.commit()
                return {"session_id": s.id}
        ns = ChatHistoryDB(
            user_email=current_user.email,
            session_title=title,
            messages=json.dumps(req.messages)
        )
        db.add(ns)
        db.commit()
        db.refresh(ns)
        return {"session_id": ns.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))

@app.get("/history/list")
def list_history(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user:
        return {"sessions": []}
    sessions = db.query(ChatHistoryDB).filter(
        ChatHistoryDB.user_email == current_user.email
    ).order_by(ChatHistoryDB.updated_at.desc()).limit(50).all()
    return {"sessions": [
        {"id": s.id, "title": s.session_title, "updated_at": s.updated_at.strftime("%b %d, %Y")}
        for s in sessions
    ]}

@app.get("/history/{session_id}")
def get_history(
    session_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(401, "Login required")
    s = db.query(ChatHistoryDB).filter(
        ChatHistoryDB.id == session_id,
        ChatHistoryDB.user_email == current_user.email
    ).first()
    if not s:
        raise HTTPException(404, "Session not found")
    return {"messages": json.loads(s.messages), "title": s.session_title}

@app.delete("/history/{session_id}")
def delete_history(
    session_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user:
        return {"error": "Login required"}
    s = db.query(ChatHistoryDB).filter(
        ChatHistoryDB.id == session_id,
        ChatHistoryDB.user_email == current_user.email
    ).first()
    if s:
        db.delete(s)
        db.commit()
    return {"message": "Deleted"}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    global db_vector
    try:
        os.makedirs("data", exist_ok=True)
        existing = [f for f in os.listdir("data") if f.endswith((".pdf", ".txt"))]
        if len(existing) >= 5:
            return {"error": "Max 5 documents. Remove one first."}

        safe = re.sub(r"[^\w.\-]", "_", file.filename)
        path = os.path.join("data", safe)
        with open(path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        all_docs, wc = [], 0
        for fn in os.listdir("data"):
            fp = os.path.join("data", fn)
            try:
                if fn.endswith(".pdf"):
                    loader = PyPDFLoader(fp)
                elif fn.endswith(".txt"):
                    loader = TextLoader(fp, encoding="utf-8")
                else:
                    continue
                docs = loader.load()
                all_docs.extend(docs)
                for d in docs:
                    wc += len(d.page_content.split())
            except Exception as e:
                print(f"[upload load error] {fn}: {e}")

        if not all_docs:
            return {"error": "Document empty or unreadable."}

        splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
        chunks   = splitter.split_documents(all_docs)
        if not chunks:
            return {"error": "No content found."}

        if os.path.exists("vectorstore"):
            shutil.rmtree("vectorstore")
        db_vector = FAISS.from_documents(chunks, embeddings)
        db_vector.save_local("vectorstore")

        return {
            "message":     f"'{file.filename}' uploaded! {len(existing)+1}/5 docs",
            "word_count":  wc,
            "chunk_count": len(chunks)
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/improve-prompt")
def improve_prompt(req: ImprovePromptRequest):
    try:
        resp = llm.invoke(
            "You are an expert prompt engineer. Rewrite this prompt to be clearer and more specific.\n"
            "RULES: Keep exact topic. Output ONLY improved prompt — no quotes, no explanation.\n\n"
            f"Original: {req.prompt}\n\nImproved:"
        )
        improved = resp.content.strip().strip('"').strip("'").strip("`")
        for pfx in ["Improved:", "Improved prompt:", "Here is", "Here's", "Result:"]:
            if improved.lower().startswith(pfx.lower()):
                improved = improved[len(pfx):].strip()
        return {"improved": improved, "original": req.prompt}
    except Exception as e:
        return {"error": str(e)}

def _hist(history, n=10):
    if not history:
        return ""
    out = "\nConversation:\n"
    for m in history[-n:]:
        out += f"{'User' if m.role=='user' else 'AI'}: {m.content[:300]}\n"
    return out

def _doc_ctx(question: str) -> str:
    if db_vector is None:
        return ""
    try:
        parts = re.split(r"[?.!]|also|and tell", question, flags=re.IGNORECASE)
        parts = [p.strip() for p in parts if len(p.strip()) > 4] + [question]
        all_docs = []
        for part in parts:
            results = db_vector.similarity_search_with_score(part, k=4)
            all_docs.extend(results)
        all_docs.sort(key=lambda x: x[1])
        seen, uniq = set(), []
        for doc, score in all_docs:
            if doc.page_content not in seen and score < 1.4:
                seen.add(doc.page_content)
                uniq.append(doc)
        return "\n\n".join([d.page_content for d in uniq[:6]]) if uniq else ""
    except Exception:
        return ""

def _suggestions(question: str) -> list:
    try:
        sr    = llm.invoke(
            f"Generate 3 short follow-up questions (max 8 words each).\n"
            f"Output ONLY valid JSON array: [\"Q1?\",\"Q2?\",\"Q3?\"]\n"
            f"Topic: {question}"
        )
        text  = re.sub(r"^```.*?\n|```$", "", sr.content.strip(), flags=re.MULTILINE).strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return [str(s).strip() for s in parsed[:3] if s and len(str(s)) > 3]
    except Exception:
        pass
    return []

def _build_prompt(fi, mi, hist, ctx, removed, question, date):
    parts = []
    if ctx:     parts.append(f"FROM DOCUMENTS:\n{ctx}")
    if removed: parts.append(f"FROM PREVIOUSLY REMOVED DOCUMENTS:\n{removed}")
    ctx_block = ("\nContext:\n" + "\n\n".join(parts) + "\n") if parts else ""
    return (
        f"You are Sonic AI — accurate, up-to-date assistant. Today: {date}.\n"
        f"{fi}\n{mi}\n"
        f"Rules: Answer exactly what was asked. No padding. Use current information.\n"
        f"For 'detail/explain' questions: be thorough and comprehensive.\n"
        f"{hist}{ctx_block}\n"
        f"Question: {question}\nAnswer:"
    )

@app.post("/ask")
def ask(req: QuestionRequest, request: Request):
    load_db()
    ip = request.client.host or "unknown"
    if not check_rate(ip):
        raise HTTPException(429, "Too many requests. Please slow down.")

    date = datetime.utcnow().strftime("%B %d, %Y")
    if is_unsafe(req.question):
        return {"answer": "I can't help with that.", "suggestions": []}

    qc = req.question.lower().strip().rstrip("!?.,")
    if qc in GREETINGS:
        return {"answer": random.choice(GREET_R), "suggestions": []}

    if any(k in req.question.lower() for k in ["what can you do","who are you","your abilities","capabilities"]):
        return {"answer": (
            "**Sonic AI — Unstoppable 1.0** ⚡\n\n"
            "• Document Q&A (PDF / TXT)\n"
            "• Real-time web search with sources\n"
            "• Full conversation memory\n"
            "• Smart format: bullets, tables, steps, code, paragraphs\n"
            "• AI prompt improver\n"
            "• Text-to-speech in 8 voices\n"
            "• Voice input\n• 8 languages\n"
            "• Effort = deep | Fast = instant"
        ), "suggestions": []}

    fmt  = detect_format(req.question, req.format_hint or "auto")
    fi   = fmt_instr(fmt, req.language or "english", req.humanize or False, req.mode or "effort")
    hist = _hist(req.history or [])
    doc  = _doc_ctx(req.question)
    rctx = req.removed_context or ""

    if req.doc_mode == "doconly" and not doc and not rctx:
        return {"answer": "Not in uploaded documents. Try **Doc + Web** mode.", "suggestions": []}

    mi = ("FAST MODE: Direct answer, max 3 sentences."
          if req.mode == "fast"
          else "EFFORT MODE: Thorough, accurate, well-structured.")

    prompt = _build_prompt(fi, mi, hist, doc, rctx, req.question, date)
    answer = llm.invoke(prompt).content.strip()
    return {"answer": answer, "suggestions": _suggestions(req.question), "format_used": fmt}

@app.post("/web-ask")
async def web_ask(req: QuestionRequest, request: Request):
    ip = request.client.host or "unknown"
    if not check_rate(ip):
        raise HTTPException(429, "Too many requests. Please slow down.")

    date = datetime.utcnow().strftime("%B %d, %Y")
    if is_unsafe(req.question):
        return {"answer": "I can't help with that.", "sources": [], "suggestions": []}

    qc = req.question.lower().strip().rstrip("!?.,")
    if qc in GREETINGS:
        return {"answer": random.choice(GREET_R), "sources": [], "suggestions": []}

    tavily = os.getenv("TAVILY_API_KEY")
    sources, web_ctx = [], ""

    if tavily:
        try:
            depth   = "advanced" if req.mode == "effort" else "basic"
            max_r   = 6 if req.mode == "effort" else 3
            timeout = 12 if req.mode == "effort" else 7
            snip    = 600 if req.mode == "effort" else 250

            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key":        tavily,
                        "query":          req.question,
                        "search_depth":   depth,
                        "max_results":    max_r,
                        "include_answer": req.mode == "effort"
                    }
                )
                data = resp.json()
                if req.mode == "effort" and data.get("answer"):
                    web_ctx += f"Summary: {data['answer']}\n\n"
                for r in data.get("results", []):
                    web_ctx += f"[{r['title']}]: {r.get('content','')[:snip]}\n\n"
                    sources.append({"title": r["title"], "url": r["url"]})
        except Exception:
            pass

    doc  = _doc_ctx(req.question)
    rctx = req.removed_context or ""
    hist = _hist(req.history or [], n=8)
    fmt  = detect_format(req.question, req.format_hint or "auto")
    fi   = fmt_instr(fmt, req.language or "english", req.humanize or False, req.mode or "effort")

    ctx_parts = []
    if doc:     ctx_parts.append(f"FROM DOCUMENTS:\n{doc}")
    if rctx:    ctx_parts.append(f"FROM REMOVED DOCUMENTS:\n{rctx}")
    if web_ctx: ctx_parts.append(f"FROM WEB ({date}):\n{web_ctx}")
    full_ctx = "\n\n".join(ctx_parts)

    mi = ("FAST MODE: Quick direct answer using web info."
          if req.mode == "fast"
          else f"EFFORT MODE: Thorough answer. Today is {date}. Cite sources naturally.")

    prompt = (
        f"You are Sonic AI — accurate assistant with real-time web access. Today: {date}.\n"
        f"{fi}\n{mi}\n"
        f"Rules: Answer only what was asked. No padding.\n"
        f"{hist}"
        + (f"\nContext:\n{full_ctx}\n" if full_ctx else "") +
        f"\nQuestion: {req.question}\nAnswer:"
    )

    answer = llm.invoke(prompt).content.strip()
    final_sources = sources[:3] if req.mode == "effort" else []
    return {"answer": answer, "sources": final_sources, "suggestions": _suggestions(req.question)}

@app.post("/summarize")
def summarize():
    load_db()
    if db_vector is None:
        return {"answer": "No document uploaded yet."}
    docs = db_vector.similarity_search("main topics summary key points overview", k=8)
    ctx  = "\n\n".join([d.page_content for d in docs])
    resp = llm.invoke(
        "Summarize this document:\n"
        "• 2-sentence overview\n• Main topics (max 7 bullets)\n• 2-3 key takeaways\n\n"
        f"Content:\n{ctx}"
    )
    return {"answer": resp.content.strip()}

@app.post("/clear")
def clear_docs():
    global db_vector
    try:
        if os.path.exists("data"):        shutil.rmtree("data")
        if os.path.exists("vectorstore"): shutil.rmtree("vectorstore")
        db_vector = None
        return {"message": "Cleared!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
def root():
    return {"status": "Sonic AI Unstoppable 1.0 ⚡"}

@app.get("/chat")
def serve():
    return FileResponse("index.html") if os.path.exists("index.html") else {"error": "index.html not found"}