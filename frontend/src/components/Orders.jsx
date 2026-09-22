function formatCreatedAt(value) {
  if (!value) return "—";

  const date = new Date(`${value.replace(" ", "T")}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}


function Orders({ orders, loading, activeOrderId, onComplete }) {
  return (
    <section className="section orders-section" id="orders">
      <div className="section__heading">
        <div>
          <p className="eyebrow">Order desk</p>
          <h2>Current orders</h2>
        </div>
        <p>Track pending purchases and mark them completed.</p>
      </div>

      {loading ? (
        <p className="state-message">Loading orders…</p>
      ) : orders.length === 0 ? (
        <div className="empty-orders">
          <span aria-hidden="true">📦</span>
          <h3>No orders yet</h3>
          <p>Your new orders will appear here.</p>
        </div>
      ) : (
        <div className="orders-list">
          {orders.map((order) => {
            const pending = order.status === "pending";
            const completing = activeOrderId === order.id;

            return (
              <article className="order-card" key={order.id}>
                <div className="order-card__title">
                  <div>
                    <span>Order #{order.id}</span>
                    <h3>{order.product_name}</h3>
                  </div>
                  <span className={`status status--${order.status}`}>
                    {order.status}
                  </span>
                </div>

                <dl className="order-card__details">
                  <div>
                    <dt>Quantity</dt>
                    <dd>{order.quantity}</dd>
                  </div>
                  <div>
                    <dt>Unit price</dt>
                    <dd>${order.unit_price.toFixed(2)}</dd>
                  </div>
                  <div>
                    <dt>Total</dt>
                    <dd>${order.total_price.toFixed(2)}</dd>
                  </div>
                  <div>
                    <dt>Created</dt>
                    <dd>{formatCreatedAt(order.created_at)}</dd>
                  </div>
                </dl>

                {pending && (
                  <button
                    className="complete-button"
                    type="button"
                    disabled={completing}
                    onClick={() => onComplete(order.id)}
                  >
                    {completing ? "Completing…" : "Complete order"}
                  </button>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}


export default Orders;
