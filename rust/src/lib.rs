//! Rust extension backing `kodo.rust_native` — see doc/BUILD.md.

use pyo3::prelude::*;

/// Demo function proving the compiled extension is built, packaged, and
/// importable end-to-end. Called from `python -m kodo --rust-hello`.
#[pyfunction]
fn hello_world() -> PyResult<String> {
    Ok("Hello from Rust!".to_string())
}

#[pymodule]
fn rust_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello_world, m)?)?;
    Ok(())
}
