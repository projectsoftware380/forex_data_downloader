def test_imports():
    import forex_data
    from forex_data.cli import app
    assert app is not None