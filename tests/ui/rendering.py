"""Map Qt Quick scene coordinates to pixels in captured window images."""

from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickWindow


def window_image_scale(window: QQuickWindow, image: QImage) -> tuple[float, float]:
    # On Windows with the software renderer, grabWindow can return a physical
    # 800 x 450 image for a logical 640 x 360 window while its image DPR is 1.
    # Measure each axis from the captured dimensions so native pixel assertions
    # inspect the intended control, including at fractional display scales.
    assert not image.isNull()
    assert window.width() > 0 and window.height() > 0

    return image.width() / window.width(), image.height() / window.height()
