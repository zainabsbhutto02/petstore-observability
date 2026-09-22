import { useState } from "react";


const PRODUCT_ICONS = {
  "Dog Food": "🦴",
  "Cat Food": "🐟",
  "Pet Toy": "🎾",
  "Dog Treats": "🥨",
  "Dog Collar": "🦮",
  "Cat Toy": "🧶"
};


function ProductCard({ product, buying, onBuy }) {
  const [quantity, setQuantity] = useState(1);
  const outOfStock = product.stock === 0;

  async function submitOrder(event) {
    event.preventDefault();
    const orderCreated = await onBuy(product.id, Number(quantity));

    if (orderCreated) {
      setQuantity(1);
    }
  }

  return (
    <article className="product-card">
      <div className="product-card__visual" aria-hidden="true">
        {PRODUCT_ICONS[product.name] || "🐾"}
      </div>

      <div className="product-card__body">
        <span className="category">{product.category}</span>
        <h3>{product.name}</h3>
        <div className="product-card__details">
          <strong>${product.price.toFixed(2)}</strong>
          <span className={outOfStock ? "stock stock--empty" : "stock"}>
            {outOfStock ? "Out of stock" : `${product.stock} in stock`}
          </span>
        </div>

        <form className="buy-form" onSubmit={submitOrder}>
          <label htmlFor={`quantity-${product.id}`}>Quantity</label>
          <div className="buy-form__controls">
            <input
              id={`quantity-${product.id}`}
              type="number"
              min="1"
              max={Math.max(product.stock, 1)}
              step="1"
              value={quantity}
              disabled={outOfStock || buying}
              onChange={(event) => setQuantity(event.target.value)}
              required
            />
            <button type="submit" disabled={outOfStock || buying}>
              {buying ? "Placing…" : "Buy"}
            </button>
          </div>
        </form>
      </div>
    </article>
  );
}


export default ProductCard;
