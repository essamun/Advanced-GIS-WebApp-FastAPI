from fastapi import FastAPI, HTTPException, Body, status, Query, Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv
import os
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from jose import JWTError, jwt  # Changed from 'import jwt' to use jose.jwt
from typing import Optional
from typing import Optional, Union

# Load environment variables
load_dotenv()

# Database connection settings
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-here")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Create FastAPI app
app = FastAPI(
    title="Business GIS API",
    description="API for managing businesses near Finch & Yonge, Toronto",
    version="0.1"
)

# Database engine
engine = create_engine(DATABASE_URL)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Pydantic models
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class BusinessCreate(BaseModel):
    name: str
    type: str
    geometry: str  # GeoJSON Point string

class BusinessUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    geometry: Optional[str] = None  # GeoJSON Point string


class User(BaseModel):
    username: str
    password: str

# Mock user database
fake_users_db = {
    "admin": {
        "username": "admin",
        "password": "secret",
    }
}


# Authentication functions
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = fake_users_db.get(username)
    if user is None:
        raise credentials_exception
    return user

@app.post("/token", response_model=Token)
async def login_for_access_token(user: User):
    if user.password != fake_users_db[user.username]["password"]:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


# Default coordinates for Finch & Yonge intersection (Toronto)
FINCH_YONGE_LAT = 43.7805
FINCH_YONGE_LON = -79.4146
DEFAULT_RADIUS = 500  # meters

@app.get("/")
async def root():
    return {
        "message": "Welcome to the Business GIS API",
        "endpoints": {
            "businesses_nearby": "/businesses/nearby",
            "create_business": "/businesses (POST)",
            "get_business": "/businesses/{id}",
            "update_business": "/businesses/{id} (PUT)",
            "delete_business": "/businesses/{id} (DELETE)",
            "docs": "/docs"
        }
    }

# CREATE Operation
@app.post("/businesses", status_code=status.HTTP_201_CREATED)
async def create_business(business: BusinessCreate):
    """Create a new business point feature near Finch & Yonge"""
    try:
        with engine.begin() as conn:
            # Insert the new business
            result = conn.execute(
                text("""
                    INSERT INTO business (name, type, wkb_geometry)
                    VALUES (
                        :name, 
                        :type,
                        ST_GeomFromGeoJSON(:geometry)
                    )
                    RETURNING ogc_fid, name, type, ST_AsGeoJSON(wkb_geometry) as geometry
                """),
                {
                    "name": business.name,
                    "type": business.type,
                    "geometry": business.geometry
                }
            )
            new_business = result.mappings().first()
            return {"status": "created", "business": new_business}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

# READ Operation (Single Business)
@app.get("/businesses/{business_id}")
async def get_business(business_id: int):
    """Get a single business by ogc_fid"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT ogc_fid, name, type, ST_AsGeoJSON(wkb_geometry) as geometry
                    FROM business
                    WHERE ogc_fid = :id
                """),
                {"id": business_id}
            ).mappings().first()
            
            if not result:
                raise HTTPException(status_code=404, detail="Business not found")
            
            return result
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

# READ Operation (Businesses near Finch & Yonge)
@app.get("/businesses/nearby")
async def businesses_nearby(
    lon: float = Query(FINCH_YONGE_LON, description="Longitude (default: Finch & Yonge)"),
    lat: float = Query(FINCH_YONGE_LAT, description="Latitude (default: Finch & Yonge)"),
    distance: float = Query(DEFAULT_RADIUS, description="Search radius in meters"),
    limit: int = Query(10, description="Maximum number of results")
):
    """Find businesses within distance meters of a point (lon/lat)"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT ogc_fid, name, type, 
                        ST_AsGeoJSON(wkb_geometry) as geometry,
                        ST_Distance(
                            wkb_geometry::geography, 
                            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                        ) as distance_meters
                    FROM business
                    WHERE ST_DWithin(
                        wkb_geometry::geography,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                        :distance
                    )
                    ORDER BY distance_meters
                    LIMIT :limit
                """),
                {
                    "lon": lon,
                    "lat": lat,
                    "distance": distance,
                    "limit": limit
                }
            ).mappings()
            
            businesses = list(result)
            return {"count": len(businesses), "businesses": businesses}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

# UPDATE Operation
@app.put("/businesses/{business_id}")
async def update_business(business_id: int, business: BusinessUpdate):
    """Update a business (partial updates allowed)"""
    try:
        with engine.begin() as conn:
            # Check if business exists first
            exists = conn.execute(
                text("SELECT 1 FROM business WHERE ogc_fid = :id"),
                {"id": business_id}
            ).scalar()
            
            if not exists:
                raise HTTPException(status_code=404, detail="Business not found")
            
            # Get current values for fields not being updated
            current = conn.execute(
                text("SELECT name, type, ST_AsGeoJSON(wkb_geometry) as geometry FROM business WHERE ogc_fid = :id"),
                {"id": business_id}
            ).mappings().first()
            
            # Prepare update values
            update_values = {
                "id": business_id,
                "name": business.name if business.name is not None else current['name'],
                "type": business.type if business.type is not None else current['type'],
                "geometry": business.geometry if business.geometry is not None else current['geometry']
            }
            
            # Perform the update
            result = conn.execute(
                text("""
                    UPDATE business
                    SET name = :name,
                        type = :type,
                        wkb_geometry = ST_GeomFromGeoJSON(:geometry)
                    WHERE ogc_fid = :id
                    RETURNING ogc_fid, name, type, ST_AsGeoJSON(wkb_geometry) as geometry
                """),
                update_values
            )
            updated_business = result.mappings().first()
            return {"status": "updated", "business": updated_business}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

# DELETE Operation
@app.delete("/businesses/{business_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_business(business_id: int):
    """Delete a business by ogc_fid"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM business WHERE ogc_fid = :id RETURNING ogc_fid"),
                {"id": business_id}
            )
            
            if not result.rowcount:
                raise HTTPException(status_code=404, detail="Business not found")
            
            return None  # 204 No Content
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))