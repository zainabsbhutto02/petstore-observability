"""SQLite setup helpers for the Online Pet Store."""

import sqlite3
from pathlib import Path


PROJECT_DIRECTORY = Path(__file__).resolve().parent
DATA_DIRECTORY = PROJECT_DIRECTORY / "data"
DATABASE_PATH = DATA_DIRECTORY / "petstore.db"


INITIAL_PRODUCTS = [
    (1, "Dog Food", "Food", 24.99, 20),
    (2, "Cat Food", "Food", 19.99, 18),
    (3, "Pet Toy", "Toys", 9.99, 15),
    (4, "Dog Treats", "Treats", 7.49, 25),
    (5, "Dog Collar", "Accessories", 14.99, 10),
    (6, "Cat Toy", "Toys", 6.99, 12),
]


def get_connection():
    """Open a connection to this project's SQLite database."""
    DATA_DIRECTORY.mkdir(exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    """Create missing tables and seed products only when none exist."""
    connection = get_connection()

    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price REAL NOT NULL,
                stock INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                total_price REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products (id)
            );
            """
        )

        product_count = connection.execute(
            "SELECT COUNT(*) AS count FROM products"
        ).fetchone()["count"]

        if product_count == 0:
            connection.executemany(
                """
                INSERT INTO products (id, name, category, price, stock)
                VALUES (?, ?, ?, ?, ?)
                """,
                INITIAL_PRODUCTS,
            )

        connection.commit()
    finally:
        connection.close()
