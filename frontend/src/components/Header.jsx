function Header() {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <a className="brand" href="#products" aria-label="Paws and Cart home">
          <span className="brand__mark" aria-hidden="true">🐾</span>
          <span>
            <strong>Paws &amp; Cart</strong>
            <small>Everyday favorites for your best friend</small>
          </span>
        </a>

        <nav aria-label="Main navigation">
          <a href="#products">Products</a>
          <a href="#orders">Orders</a>
        </nav>
      </div>
    </header>
  );
}


export default Header;
