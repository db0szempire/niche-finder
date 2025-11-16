from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, validator
import sqlite3
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import csv
import io
from typing import Optional, List
import re
from contextlib import contextmanager

load_dotenv()

app = FastAPI(title="Niche Finder Waitlist API")

# Parse CORS origins (supports comma-separated URLs)
frontend_urls = os.getenv("FRONTEND_URL", "*")
allowed_origins = [url.strip() for url in frontend_urls.split(",")] if frontend_urls != "*" else ["*"]

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
DATABASE_PATH = os.getenv("DATABASE_PATH", "waitlist.db")

def init_db():
    """Initialize the database with required tables"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Subscribers table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            whatsapp TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            x_handle TEXT,
            niche TEXT,
            pro_tier_eligible BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Admin activity logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Message logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER,
            message_content TEXT,
            status TEXT,
            error_message TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subscriber_id) REFERENCES subscribers(id)
        )
    """)
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()

@contextmanager
def get_db():
    """Database context manager"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    try:
        yield conn
    finally:
        conn.close()

# Models
class SubscriberCreate(BaseModel):
    whatsapp: str
    email: EmailStr
    x_handle: Optional[str] = None
    niche: Optional[str] = None

    @validator('whatsapp')
    def validate_whatsapp(cls, v):
        cleaned = re.sub(r'[\s\-\(\)]', '', v)
        if not re.match(r'^\+?[\d]{10,15}$', cleaned):
            raise ValueError('Invalid WhatsApp number format')
        return cleaned

class BulkMessageRequest(BaseModel):
    message: str
    subscriber_ids: Optional[List[int]] = None

# Admin authentication
def verify_admin_password(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    try:
        scheme, password = authorization.split()
        if scheme.lower() != "bearer" or password != os.getenv("ADMIN_PASSWORD"):
            raise HTTPException(status_code=401, detail="Invalid credentials")
    except:
        raise HTTPException(status_code=401, detail="Invalid authorization format")

# Helper function to convert Row to dict
def row_to_dict(row):
    return dict(zip(row.keys(), row)) if row else None

def rows_to_list(rows):
    return [dict(zip(row.keys(), row)) for row in rows]

# Routes
@app.get("/")
def root():
    environment = os.getenv("ENVIRONMENT", "production")
    return {
        "message": "Niche Finder Waitlist API",
        "status": "active",
        "environment": environment,
        "database": "SQLite",
        "cors_origins": allowed_origins
    }

@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/api/waitlist/subscribe")
def subscribe_to_waitlist(subscriber: SubscriberCreate):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            # Check if already exists
            cursor.execute(
                "SELECT id FROM subscribers WHERE email = ? OR whatsapp = ?",
                (subscriber.email.lower(), subscriber.whatsapp)
            )
            
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail="You're already on the waitlist!")
            
            # Determine pro tier eligibility
            pro_tier = 1 if subscriber.x_handle else 0
            
            # Insert subscriber
            cursor.execute(
                """
                INSERT INTO subscribers (whatsapp, email, x_handle, niche, pro_tier_eligible, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subscriber.whatsapp,
                    subscriber.email.lower(),
                    subscriber.x_handle,
                    subscriber.niche,
                    pro_tier,
                    datetime.now(),
                    datetime.now()
                )
            )
            
            subscriber_id = cursor.lastrowid
            
            # Log admin activity
            cursor.execute(
                "INSERT INTO admin_activity (action, details) VALUES (?, ?)",
                ("new_subscriber", f"New subscriber: {subscriber.email}")
            )
            
            conn.commit()
            
            return {
                "success": True,
                "message": "Successfully joined the waitlist!",
                "pro_tier_eligible": bool(pro_tier),
                "subscriber_id": subscriber_id
            }
            
        except HTTPException:
            raise
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Email or WhatsApp already registered!")
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/subscribers")
def get_all_subscribers(_: None = Depends(verify_admin_password)):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                SELECT id, whatsapp, email, x_handle, niche, pro_tier_eligible, 
                       created_at, updated_at
                FROM subscribers
                ORDER BY created_at DESC
                """
            )
            
            subscribers = rows_to_list(cursor.fetchall())
            
            # Convert boolean values
            for sub in subscribers:
                sub['pro_tier_eligible'] = bool(sub['pro_tier_eligible'])
            
            return {
                "success": True,
                "count": len(subscribers),
                "subscribers": subscribers
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/stats")
def get_stats(_: None = Depends(verify_admin_password)):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            # Get all subscribers
            cursor.execute(
                """
                SELECT id, whatsapp, email, x_handle, niche, pro_tier_eligible, 
                       created_at, updated_at
                FROM subscribers
                ORDER BY created_at DESC
                """
            )
            subscribers = rows_to_list(cursor.fetchall())
            
            # Convert boolean values and parse dates
            for sub in subscribers:
                sub['pro_tier_eligible'] = bool(sub['pro_tier_eligible'])
                # Parse created_at string to datetime
                if isinstance(sub['created_at'], str):
                    sub['created_at'] = datetime.fromisoformat(sub['created_at'])
            
            # Calculate stats
            total = len(subscribers)
            pro_tier = sum(1 for sub in subscribers if sub['pro_tier_eligible'])
            
            # Today's signups
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today = sum(1 for sub in subscribers if sub['created_at'] >= today_start)
            
            # This week's signups
            week_ago = datetime.now() - timedelta(days=7)
            week = sum(1 for sub in subscribers if sub['created_at'] >= week_ago)
            
            return {
                "success": True,
                "stats": {
                    "total_subscribers": total,
                    "pro_tier_eligible": pro_tier,
                    "signups_today": today,
                    "signups_this_week": week
                },
                "subscribers": subscribers
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/export/csv")
def export_csv(_: None = Depends(verify_admin_password)):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                SELECT id, whatsapp, email, x_handle, niche, pro_tier_eligible, 
                       created_at, updated_at
                FROM subscribers
                ORDER BY created_at DESC
                """
            )
            
            subscribers = rows_to_list(cursor.fetchall())
            
            if not subscribers:
                raise HTTPException(status_code=404, detail="No subscribers to export")
            
            # Create CSV in memory
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=subscribers[0].keys())
            writer.writeheader()
            writer.writerows(subscribers)
            
            # Return as downloadable file
            output.seek(0)
            return StreamingResponse(
                iter([output.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=waitlist_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
            )
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/export/sql")
def export_sql(_: None = Depends(verify_admin_password)):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                SELECT id, whatsapp, email, x_handle, niche, pro_tier_eligible, 
                       created_at, updated_at
                FROM subscribers
                ORDER BY created_at DESC
                """
            )
            
            subscribers = rows_to_list(cursor.fetchall())
            
            # Generate SQL INSERT statements
            sql_content = "-- Waitlist Subscribers Export\n"
            sql_content += f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            sql_content += "CREATE TABLE IF NOT EXISTS subscribers (\n"
            sql_content += "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            sql_content += "    whatsapp TEXT NOT NULL,\n"
            sql_content += "    email TEXT NOT NULL,\n"
            sql_content += "    x_handle TEXT,\n"
            sql_content += "    niche TEXT,\n"
            sql_content += "    pro_tier_eligible BOOLEAN DEFAULT 0,\n"
            sql_content += "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n"
            sql_content += "    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n"
            sql_content += ");\n\n"
            
            for sub in subscribers:
                x_handle_val = "NULL" if not sub['x_handle'] else f"'{sub['x_handle']}'"
                niche_val = "NULL" if not sub['niche'] else f"'{sub['niche'].replace(chr(39), chr(39)+chr(39))}'"
                
                sql_content += f"INSERT INTO subscribers (id, whatsapp, email, x_handle, niche, pro_tier_eligible, created_at, updated_at) VALUES "
                sql_content += f"({sub['id']}, '{sub['whatsapp']}', '{sub['email']}', "
                sql_content += f"{x_handle_val}, "
                sql_content += f"{niche_val}, "
                sql_content += f"{sub['pro_tier_eligible']}, '{sub['created_at']}', '{sub['updated_at']}');\n"
            
            # Return as downloadable file
            return StreamingResponse(
                iter([sql_content]),
                media_type="application/sql",
                headers={"Content-Disposition": f"attachment; filename=waitlist_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"}
            )
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/send-whatsapp")
def send_bulk_whatsapp(request: BulkMessageRequest, _: None = Depends(verify_admin_password)):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            # Get subscribers to message
            if request.subscriber_ids:
                placeholders = ','.join('?' * len(request.subscriber_ids))
                cursor.execute(
                    f"SELECT id, whatsapp FROM subscribers WHERE id IN ({placeholders})",
                    request.subscriber_ids
                )
            else:
                cursor.execute("SELECT id, whatsapp FROM subscribers")
            
            subscribers = rows_to_list(cursor.fetchall())
            
            # Check if Twilio is configured
            twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
            twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
            twilio_from = os.getenv("TWILIO_WHATSAPP_FROM")
            
            if not all([twilio_sid, twilio_token, twilio_from]):
                raise HTTPException(
                    status_code=500,
                    detail="Twilio not configured. Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM environment variables."
                )
            
            # Import Twilio
            try:
                from twilio.rest import Client
                
                client = Client(twilio_sid, twilio_token)
                
                success_count = 0
                failed_count = 0
                error_messages = []
                
                for sub in subscribers:
                    try:
                        # Send WhatsApp message
                        message = client.messages.create(
                            from_=twilio_from,
                            body=request.message,
                            to=f"whatsapp:{sub['whatsapp']}"
                        )
                        
                        # Log success
                        cursor.execute(
                            """
                            INSERT INTO message_logs (subscriber_id, message_content, status, sent_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (sub['id'], request.message, 'sent', datetime.now())
                        )
                        success_count += 1
                        
                    except Exception as e:
                        error_msg = str(e)
                        error_messages.append(f"Failed to send to {sub['whatsapp']}: {error_msg}")
                        
                        # Log failure
                        cursor.execute(
                            """
                            INSERT INTO message_logs (subscriber_id, message_content, status, error_message, sent_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (sub['id'], request.message, 'failed', error_msg, datetime.now())
                        )
                        failed_count += 1
                
                conn.commit()
                
                return {
                    "success": True,
                    "message": f"Messages sent: {success_count} successful, {failed_count} failed",
                    "stats": {
                        "total": len(subscribers),
                        "success": success_count,
                        "failed": failed_count
                    },
                    "errors": error_messages if error_messages else None
                }
                
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="Twilio package not installed. Add 'twilio' to requirements.txt and redeploy."
                )
            
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/admin/subscribers/{subscriber_id}")
def delete_subscriber(subscriber_id: int, _: None = Depends(verify_admin_password)):
    with get_db() as conn:
        cursor = conn.cursor()
        
        try:
            cursor.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
            
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Subscriber not found")
            
            conn.commit()
            
            return {"success": True, "message": "Subscriber deleted successfully"}
            
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)