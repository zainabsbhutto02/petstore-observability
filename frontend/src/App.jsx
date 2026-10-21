import { useEffect, useState } from "react";

import Header from "./components/Header.jsx";
import Orders from "./components/Orders.jsx";
import ProductCard from "./components/ProductCard.jsx";


async function readResponse(response) {
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    if (response.status === 429) {
      // Rate limited: tell the user exactly when they may try again.
      const seconds = response.headers.get("Retry-After") || data.retry_after_seconds;
      throw new Error(
        `Too many orders too quickly. Please try again in ${seconds || "a few"} seconds.`
      );
    }
    throw new Error(data.error || "The request could not be completed.");
  }

  return data;
}


// One new key per click on "Buy". If the network drops and we retry, the retry
// reuses the same key, so the server returns the same order instead of making a second one.
function newIdempotencyKey() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}


function App() {
  const [products, setProducts] = useState([]);
  const [orders, setOrders] = useState([]);
  const [productsLoading, setProductsLoading] = useState(true);
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [activeProductId, setActiveProductId] = useState(null);
  const [activeOrderId, setActiveOrderId] = useState(null);
  const [message, setMessage] = useState(null);

  async function loadProducts() {
    setProductsLoading(true);
    try {
      const response = await fetch("/products");
      setProducts(await readResponse(response));
      return true;
    } catch {
      setMessage({
        type: "error",
        text: "Products could not be loaded. Make sure the Pet Store backend is running."
      });
      return false;
    } finally {
      setProductsLoading(false);
    }
  }

  async function loadOrders() {
    setOrdersLoading(true);
    try {
      const response = await fetch("/orders");
      setOrders(await readResponse(response));
      return true;
    } catch {
      setMessage({
        type: "error",
        text: "Orders could not be loaded. Make sure the Pet Store backend is running."
      });
      return false;
    } finally {
      setOrdersLoading(false);
    }
  }

  useEffect(() => {
    loadProducts();
    loadOrders();
  }, []);

  async function handleBuy(productId, quantity) {
    setActiveProductId(productId);
    setMessage(null);

    const idempotencyKey = newIdempotencyKey();
    const request = () =>
      fetch("/orders", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey
        },
        body: JSON.stringify({ product_id: productId, quantity })
      });

    try {
      let response;
      try {
        response = await request();
      } catch {
        // Network failure (no response at all): safe to retry once because of the key.
        response = await request();
      }
      const order = await readResponse(response);

      await Promise.all([loadProducts(), loadOrders()]);
      setMessage({
        type: "success",
        text: `Order #${order.id} was placed successfully.`
      });
      return true;
    } catch (error) {
      setMessage({ type: "error", text: error.message });
      return false;
    } finally {
      setActiveProductId(null);
    }
  }

  async function handleComplete(orderId) {
    setActiveOrderId(orderId);
    setMessage(null);

    try {
      const response = await fetch(`/orders/${orderId}/complete`, {
        method: "POST"
      });
      await readResponse(response);

      await loadOrders();
      setMessage({
        type: "success",
        text: `Order #${orderId} is now completed.`
      });
    } catch (error) {
      setMessage({ type: "error", text: error.message });
    } finally {
      setActiveOrderId(null);
    }
  }

  return (
    <>
      <Header />

      <main>
        {message && (
          <div className={`message message--${message.type}`} role="status">
            <span>{message.text}</span>
            <button
              className="message__close"
              type="button"
              aria-label="Dismiss message"
              onClick={() => setMessage(null)}
            >
              ×
            </button>
          </div>
        )}

        <section className="section" id="products">
          <div className="section__heading">
            <div>
              <p className="eyebrow">Shop essentials</p>
              <h2>Products for happy pets</h2>
            </div>
            <p>Choose a quantity and place an order in a few clicks.</p>
          </div>

          {productsLoading ? (
            <p className="state-message">Loading products…</p>
          ) : products.length === 0 ? (
            <p className="state-message">No products are available.</p>
          ) : (
            <div className="product-grid">
              {products.map((product) => (
                <ProductCard
                  key={product.id}
                  product={product}
                  buying={activeProductId === product.id}
                  onBuy={handleBuy}
                />
              ))}
            </div>
          )}
        </section>

        <Orders
          orders={orders}
          loading={ordersLoading}
          activeOrderId={activeOrderId}
          onComplete={handleComplete}
        />
      </main>

      <footer>
        <p>Paws &amp; Cart · Simple care for every companion</p>
      </footer>
    </>
  );
}


export default App;