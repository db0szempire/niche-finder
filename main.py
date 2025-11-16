from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, validator
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv
from datetime import datetime
import csv
import io
from typing import Optional, List
import re

load_dotenv()

app = FastAPI(title="Niche Finder Waitlist API")

# Parse CORS origins (supports comma-separated URLs)
frontend_urls = os.getenv("FRONTEND_URL", "*")
allowed_origins = [url.strip() for url in frontend_urls.split(",")] if frontend_urls != "*" else ["*"]

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,  # Supports multiple origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database connection
def get_db():
    conn = psycopg2.connect(
        os.getenv("DATABASE_URL"),
        cursor_factory=RealDictCursor
    )
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
        # Remove spaces, dashes, parentheses
        cleaned = re.sub(r'[\s\-\(\)]', '', v)
        if not re.match(r'^\+?[\d]{10,15}$', cleaned):
            raise ValueError('Invalid WhatsApp number format')
        return cleaned

class BulkMessageRequest(BaseModel):
    message: str
    subscriber_ids: Optional[List[int]] = None  # If None, send to all

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

# Routes
@app.get("/")
def root():
    environment = os.getenv("ENVIRONMENT", "production")
    return {
        "message": "Niche Finder Waitlist API",
        "status": "active",
        "environment": environment,
        "cors_origins": allowed_origins
    }

@app.get("/health")
def health_check():
    """Health check endpoint for Railway"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/api/waitlist/subscribe")
def subscribe_to_waitlist(subscriber: SubscriberCreate, conn = Depends(get_db)):
    cursor = conn.cursor()
    
    try:
        # Check if already exists
        cursor.execute(
            "SELECT id FROM subscribers WHERE email = %s OR whatsapp = %s",
            (subscriber.email.lower(), subscriber.whatsapp)
        )
        
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="You're already on the waitlist!")
        
        # Determine pro tier eligibility
        pro_tier = bool(subscriber.x_handle)
        
        # Insert subscriber
        cursor.execute(
            """
            INSERT INTO subscribers (whatsapp, email, x_handle, niche, pro_tier_eligible, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                subscriber.whatsapp,
                subscriber.email.lower(),
                subscriber.x_handle,
                subscriber.niche,
                pro_tier,
                datetime.now()
            )
        )
        
        subscriber_id = cursor.fetchone()['id']
        conn.commit()
        
        # Log admin activity
        cursor.execute(
            "INSERT INTO admin_activity (action, details) VALUES (%s, %s)",
            ("new_subscriber", f"New subscriber: {subscriber.email}")
        )
        conn.commit()
        
        return {
            "success": True,
            "message": "Successfully joined the waitlist!",
            "pro_tier_eligible": pro_tier,
            "subscriber_id": subscriber_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

@app.get("/api/admin/subscribers")
def get_all_subscribers(conn = Depends(get_db), _: None = Depends(verify_admin_password)):
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
        
        subscribers = cursor.fetchall()
        
        return {
            "success": True,
            "count": len(subscribers),
            "subscribers": subscribers
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

@app.get("/api/admin/stats")
def get_stats(conn = Depends(get_db), _: None = Depends(verify_admin_password)):
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
        subscribers = cursor.fetchall()
        
        # Total subscribers
        total = len(subscribers)
        
        # Pro tier eligible
        pro_tier = sum(1 for sub in subscribers if sub['pro_tier_eligible'])
        
        # Today's signups
        today = sum(1 for sub in subscribers if sub['created_at'].date() == datetime.now().date())
        
        # This week's signups
        week_ago = datetime.now() - datetime.timedelta(days=7)
        week = sum(1 for sub in subscribers if sub['created_at'] >= week_ago)
        
        return {
            "success": True,
            "stats": {
                "total_subscribers": total,
                "pro_tier_eligible": pro_tier,
                "signups_today": today,
                "signups_this_week": week
            },
            "subscribers": subscribers  # Include full list for admin dashboard
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

@app.get("/api/admin/export/csv")
def export_csv(conn = Depends(get_db), _: None = Depends(verify_admin_password)):
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
        
        subscribers = cursor.fetchall()
        
        # Create CSV in memory
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=subscribers[0].keys() if subscribers else [])
        writer.writeheader()
        writer.writerows(subscribers)
        
        # Return as downloadable file
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=waitlist_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

@app.get("/api/admin/export/sql")
def export_sql(conn = Depends(get_db), _: None = Depends(verify_admin_password)):
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
        
        subscribers = cursor.fetchall()
        
        # Generate SQL INSERT statements
        sql_content = "-- Waitlist Subscribers Export\n"
        sql_content += f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        sql_content += "CREATE TABLE IF NOT EXISTS subscribers (\n"
        sql_content += "    id SERIAL PRIMARY KEY,\n"
        sql_content += "    whatsapp VARCHAR(20) NOT NULL,\n"
        sql_content += "    email VARCHAR(255) NOT NULL,\n"
        sql_content += "    x_handle VARCHAR(100),\n"
        sql_content += "    niche TEXT,\n"
        sql_content += "    pro_tier_eligible BOOLEAN DEFAULT FALSE,\n"
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
    finally:
        cursor.close()

@app.post("/api/admin/send-whatsapp")
def send_bulk_whatsapp(request: BulkMessageRequest, conn = Depends(get_db), _: None = Depends(verify_admin_password)):
    cursor = conn.cursor()
    
    try:
        # Get subscribers to message
        if request.subscriber_ids:
            cursor.execute(
                "SELECT id, whatsapp FROM subscribers WHERE id = ANY(%s)",
                (request.subscriber_ids,)
            )
        else:
            cursor.execute("SELECT id, whatsapp FROM subscribers")
        
        subscribers = cursor.fetchall()
        
        # Import Twilio (optional - install twilio package)
        try:
            from twilio.rest import Client
            
            client = Client(
                os.getenv("TWILIO_ACCOUNT_SID"),
                os.getenv("TWILIO_AUTH_TOKEN")
            )
            
            success_count = 0
            failed_count = 0
            
            for sub in subscribers:
                try:
                    # Send WhatsApp message
                    message = client.messages.create(
                        from_=os.getenv("TWILIO_WHATSAPP_FROM"),
                        body=request.message,
                        to=f"whatsapp:{sub['whatsapp']}"
                    )
                    
                    # Log success
                    cursor.execute(
                        """
                        INSERT INTO message_logs (subscriber_id, message_content, status, sent_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (sub['id'], request.message, 'sent', datetime.now())
                    )
                    success_count += 1
                    
                except Exception as e:
                    # Log failure
                    cursor.execute(
                        """
                        INSERT INTO message_logs (subscriber_id, message_content, status, error_message, sent_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (sub['id'], request.message, 'failed', str(e), datetime.now())
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
                }
            }
            
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="Twilio not configured. Install 'twilio' package and set environment variables."
            )
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

@app.delete("/api/admin/subscribers/{subscriber_id}")
def delete_subscriber(subscriber_id: int, conn = Depends(get_db), _: None = Depends(verify_admin_password)):
    cursor = conn.cursor()
    
    try:
        cursor.execute("DELETE FROM subscribers WHERE id = %s RETURNING id", (subscriber_id,))
        deleted = cursor.fetchone()
        
        if not deleted:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        
        conn.commit()
        
        return {"success": True, "message": "Subscriber deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)