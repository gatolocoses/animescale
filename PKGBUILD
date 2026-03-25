# Maintainer: gatolocoses <gatolocoses@gmail.com>
pkgname=animescale
pkgver=1.0.2
pkgrel=1
pkgdesc="AI anime upscaling pipeline using Real-ESRGAN and FFmpeg"
arch=('x86_64')
url="https://github.com/gatolocoses/animescale"
license=('MIT')
depends=('python>=3.11' 'realesrgan-ncnn-vulkan' 'ffmpeg')
optdepends=(
    'lm_sensors: CPU/GPU temperature display in monitor'
    'nvidia-utils: NVIDIA GPU utilization display in monitor'
)
makedepends=('python-hatchling' 'python-build' 'python-installer')
source=("git+$url.git#tag=v$pkgver")
sha256sums=('SKIP')

build() {
    cd "$pkgname"
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
