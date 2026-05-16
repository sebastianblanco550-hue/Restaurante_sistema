from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Optional
import json
import uuid
import sqlite3
import hashlib

app = FastAPI(title="SaaS Restaurante API - Fase 4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "saas.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Restaurantes (Usuarios Admin)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS restaurants (
            id TEXT PRIMARY KEY,
            name TEXT,
            username TEXT UNIQUE,
            password_hash TEXT,
            subscription_active BOOLEAN DEFAULT 0
        )
    ''')
    
    # Personal (Staff)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS staff (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT,
            name TEXT,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT
        )
    ''')
    
    # Mesas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tables (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT,
            name TEXT
        )
    ''')
    
    # Menú
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS menu_items (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT,
            name TEXT,
            price REAL,
            category TEXT
        )
    ''')
    
    # Órdenes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT,
            table_name TEXT,
            waiter_name TEXT,
            status TEXT,
            total_amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id TEXT PRIMARY KEY,
            order_id TEXT,
            item_name TEXT,
            quantity INTEGER,
            price REAL,
            notes TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# --- MODELOS PYDANTIC ---
class RegisterRequest(BaseModel):
    name: str
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class StaffCreate(BaseModel):
    name: str
    username: str
    password: str
    role: str

class TableCreate(BaseModel):
    name: str

class MenuItemCreate(BaseModel):
    name: str
    price: float
    category: str = "General"

class OrderItemInput(BaseModel):
    item_name: str
    quantity: int
    price: float
    notes: str = ""

class OrderCreate(BaseModel):
    restaurant_id: str
    table_name: str
    waiter_name: str
    items: List[OrderItemInput]

# --- UTILIDADES ---
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# --- WEBSOCKETS MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, restaurant_id: str):
        await websocket.accept()
        if restaurant_id not in self.active_connections:
            self.active_connections[restaurant_id] = []
        self.active_connections[restaurant_id].append(websocket)

    def disconnect(self, websocket: WebSocket, restaurant_id: str):
        if restaurant_id in self.active_connections:
            self.active_connections[restaurant_id].remove(websocket)
            if not self.active_connections[restaurant_id]:
                del self.active_connections[restaurant_id]

    async def broadcast_to_restaurant(self, restaurant_id: str, message: dict):
        if restaurant_id in self.active_connections:
            for connection in self.active_connections[restaurant_id]:
                await connection.send_text(json.dumps(message))

manager = ConnectionManager()

# --- ENDPOINTS AUTH Y SUSCRIPCIÓN ---

@app.post("/api/register")
async def register(req: RegisterRequest):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM restaurants WHERE username = ?", (req.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="El usuario de restaurante ya existe")
        
    rest_id = str(uuid.uuid4())
    hashed_pwd = hash_password(req.password)
    
    cursor.execute('''
        INSERT INTO restaurants (id, name, username, password_hash, subscription_active)
        VALUES (?, ?, ?, ?, ?)
    ''', (rest_id, req.name, req.username, hashed_pwd, False))
    conn.commit()
    conn.close()
    
    return {"success": True, "restaurant_id": rest_id, "message": "Restaurante registrado"}

@app.post("/api/login")
async def login(req: LoginRequest):
    conn = get_db()
    cursor = conn.cursor()
    hashed_pwd = hash_password(req.password)
    
    # 1. Buscar en Restaurants (Admin)
    cursor.execute("SELECT id, name, subscription_active FROM restaurants WHERE username = ? AND password_hash = ?", (req.username, hashed_pwd))
    rest_row = cursor.fetchone()
    
    if rest_row:
        conn.close()
        rest_dict = dict(rest_row)
        if not rest_dict["subscription_active"]:
            return {
                "success": False, "needs_payment": True, "restaurant_id": rest_dict["id"],
                "message": "Suscripción inactiva"
            }
        return {
            "success": True, "restaurant_id": rest_dict["id"], "restaurant_name": rest_dict["name"],
            "role": "admin", "token": f"jwt-{rest_dict['id']}", "user_name": "Administrador"
        }
        
    # 2. Si no es admin, buscar en Staff
    cursor.execute("SELECT id, restaurant_id, name, role FROM staff WHERE username = ? AND password_hash = ?", (req.username, hashed_pwd))
    staff_row = cursor.fetchone()
    
    if staff_row:
        staff_dict = dict(staff_row)
        # Verificar que el restaurante dueño de este staff tenga suscripción activa
        cursor.execute("SELECT name, subscription_active FROM restaurants WHERE id = ?", (staff_dict["restaurant_id"],))
        parent_rest = dict(cursor.fetchone())
        conn.close()
        
        if not parent_rest["subscription_active"]:
             return {
                "success": False, "needs_payment": True, "restaurant_id": staff_dict["restaurant_id"],
                "message": "La suscripción de este restaurante está inactiva"
            }
            
        return {
            "success": True, "restaurant_id": staff_dict["restaurant_id"], "restaurant_name": parent_rest["name"],
            "role": staff_dict["role"], "token": f"jwt-staff-{staff_dict['id']}", "user_name": staff_dict["name"]
        }

    conn.close()
    raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

@app.post("/api/subscription/pay/{restaurant_id}")
async def pay_subscription(restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE restaurants SET subscription_active = 1 WHERE id = ?", (restaurant_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Suscripción activada"}

# --- STAFF ---
@app.get("/api/staff/{restaurant_id}")
async def get_staff(restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, username, role FROM staff WHERE restaurant_id = ?", (restaurant_id,))
    staff = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return staff

@app.post("/api/staff/{restaurant_id}")
async def add_staff(restaurant_id: str, staff: StaffCreate):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM staff WHERE username = ?", (staff.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Este nombre de usuario de personal ya existe")
        
    staff_id = str(uuid.uuid4())
    hashed_pwd = hash_password(staff.password)
    
    cursor.execute('''
        INSERT INTO staff (id, restaurant_id, name, username, password_hash, role)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (staff_id, restaurant_id, staff.name, staff.username, hashed_pwd, staff.role))
    conn.commit()
    conn.close()
    return {"success": True, "id": staff_id}

@app.delete("/api/staff/{staff_id}")
async def delete_staff(staff_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    conn.commit()
    conn.close()
    return {"success": True}

# --- MESAS ---
@app.get("/api/tables/{restaurant_id}")
async def get_tables(restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tables WHERE restaurant_id = ?", (restaurant_id,))
    tables = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return tables

@app.post("/api/tables/{restaurant_id}")
async def add_table(restaurant_id: str, table: TableCreate):
    conn = get_db()
    cursor = conn.cursor()
    table_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO tables (id, restaurant_id, name)
        VALUES (?, ?, ?)
    ''', (table_id, restaurant_id, table.name))
    conn.commit()
    conn.close()
    return {"success": True, "id": table_id}

@app.delete("/api/tables/{table_id}")
async def delete_table(table_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tables WHERE id = ?", (table_id,))
    conn.commit()
    conn.close()
    return {"success": True}

# --- MENÚ ---
@app.get("/api/menu/{restaurant_id}")
async def get_menu(restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM menu_items WHERE restaurant_id = ?", (restaurant_id,))
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items

@app.post("/api/menu/{restaurant_id}")
async def add_menu_item(restaurant_id: str, item: MenuItemCreate):
    conn = get_db()
    cursor = conn.cursor()
    item_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO menu_items (id, restaurant_id, name, price, category)
        VALUES (?, ?, ?, ?, ?)
    ''', (item_id, restaurant_id, item.name, item.price, item.category))
    conn.commit()
    conn.close()
    return {"success": True, "id": item_id}

@app.delete("/api/menu/{item_id}")
async def delete_menu_item(item_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return {"success": True}

# --- ÓRDENES ---
@app.get("/api/orders/{restaurant_id}")
async def get_active_orders(restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE restaurant_id = ? AND status != 'completed' ORDER BY created_at DESC", (restaurant_id,))
    orders_rows = cursor.fetchall()
    
    orders = []
    for row in orders_rows:
        order_dict = dict(row)
        cursor.execute("SELECT * FROM order_items WHERE order_id = ?", (order_dict["id"],))
        order_dict["items"] = [dict(i) for i in cursor.fetchall()]
        orders.append(order_dict)
        
    conn.close()
    return orders

@app.post("/api/orders")
async def create_order(order: OrderCreate):
    conn = get_db()
    cursor = conn.cursor()
    
    order_id = str(uuid.uuid4())
    total_amount = sum([item.quantity * item.price for item in order.items])
    
    cursor.execute('''
        INSERT INTO orders (id, restaurant_id, table_name, waiter_name, status, total_amount)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (order_id, order.restaurant_id, order.table_name, order.waiter_name, "pending", total_amount))
    
    for item in order.items:
        item_id = str(uuid.uuid4())
        cursor.execute('''
            INSERT INTO order_items (id, order_id, item_name, quantity, price, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (item_id, order_id, item.item_name, item.quantity, item.price, item.notes))
    
    conn.commit()
    conn.close()
    
    order_data = order.model_dump()
    order_data["id"] = order_id
    order_data["status"] = "pending"
    order_data["total_amount"] = total_amount
    
    await manager.broadcast_to_restaurant(
        restaurant_id=order.restaurant_id,
        message={"type": "NEW_ORDER", "data": order_data}
    )
    
    return {"success": True, "order": order_data}

@app.put("/api/orders/{order_id}/ready")
async def mark_order_ready(order_id: str, restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = 'ready' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    
    await manager.broadcast_to_restaurant(
        restaurant_id=restaurant_id,
        message={"type": "ORDER_UPDATED", "data": {"order_id": order_id, "status": "ready"}}
    )
    return {"success": True}

@app.put("/api/orders/{order_id}/completed")
async def mark_order_completed(order_id: str, restaurant_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@app.websocket("/ws/kitchen/{restaurant_id}")
async def websocket_kitchen_endpoint(websocket: WebSocket, restaurant_id: str):
    await manager.connect(websocket, restaurant_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, restaurant_id)
