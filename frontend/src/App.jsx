import { useEffect, useState } from "react";

import Header from "./components/Header.jsx";
import Orders from "./components/Orders.jsx";
import ProductCard from "./components/ProductCard.jsx";


async function readResponse(response) {
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.error || "The request could not be completed.");
  }

  return data;
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

    try {
      const response = await fetch("/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: productId, quantity })
      });
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
