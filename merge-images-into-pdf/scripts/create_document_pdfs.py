#!/usr/bin/env python3
"""Create rotation-only and scanner-style PDFs from document page photos on macOS."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SWIFT_SOURCE = r'''
import AppKit
import CoreGraphics
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import Vision

struct Manifest: Decodable {
    let inputs: [String]
    let rotations: [Int]
    let outputDir: String
    let basename: String
    let jpegQuality: Double
    let saturation: Double
    let contrast: Double
    let brightness: Double
    let sharpness: Double
    let minimumConfidence: Float
    let minimumSize: Float
    let corners: [String: [Double]]
}

guard let manifestArgument = CommandLine.arguments.last else {
    fatalError("Missing manifest path")
}
let manifestURL = URL(fileURLWithPath: manifestArgument)
let data = try Data(contentsOf: manifestURL)
let manifest = try JSONDecoder().decode(Manifest.self, from: data)
let ciContext = CIContext(options: [.useSoftwareRenderer: false])
let fm = FileManager.default
let outputDir = URL(fileURLWithPath: manifest.outputDir, isDirectory: true)
let rotatedDir = outputDir.appendingPathComponent("rotated-images", isDirectory: true)
let scannedDir = outputDir.appendingPathComponent("scanned-images", isDirectory: true)
try fm.createDirectory(at: rotatedDir, withIntermediateDirectories: true)
try fm.createDirectory(at: scannedDir, withIntermediateDirectories: true)

struct PageResult: Encodable {
    let page: Int
    let input: String
    let rotation: Int
    let rotatedImage: String
    let scannedImage: String
    let scannerMode: String
    let confidence: Float?
}

func normalizedRotation(_ degrees: Int) -> Int {
    let value = ((degrees % 360) + 360) % 360
    if [0, 90, 180, 270].contains(value) { return value }
    fatalError("Rotation must be one of 0, 90, 180, or 270: \(degrees)")
}

func orient(_ image: CIImage, clockwise degrees: Int) -> CIImage {
    let rotation = normalizedRotation(degrees)
    let oriented: CIImage
    switch rotation {
    case 0:
        oriented = image
    case 90:
        oriented = image.oriented(.right)
    case 180:
        oriented = image.oriented(.down)
    case 270:
        oriented = image.oriented(.left)
    default:
        fatalError("Unexpected rotation")
    }
    let extent = oriented.extent.integral
    return oriented.transformed(by: CGAffineTransform(translationX: -extent.origin.x, y: -extent.origin.y))
}

func writeJPEG(_ image: CIImage, to url: URL) throws {
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
        fatalError("Could not create sRGB color space")
    }
    let extent = image.extent.integral
    let normalized = image.cropped(to: extent).transformed(by: CGAffineTransform(translationX: -extent.origin.x, y: -extent.origin.y))
    try ciContext.writeJPEGRepresentation(
        of: normalized,
        to: url,
        colorSpace: colorSpace,
        options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: manifest.jpegQuality]
    )
}

func pointFromTop(_ values: [Double], _ index: Int, height: CGFloat) -> CGPoint {
    CGPoint(x: values[index], y: Double(height) - values[index + 1])
}

func normalizedPoint(_ p: CGPoint, extent: CGRect) -> CGPoint {
    CGPoint(x: extent.minX + p.x * extent.width, y: extent.minY + p.y * extent.height)
}

func rectangleArea(_ observation: VNRectangleObservation) -> CGFloat {
    let xs = [observation.topLeft.x, observation.topRight.x, observation.bottomLeft.x, observation.bottomRight.x]
    let ys = [observation.topLeft.y, observation.topRight.y, observation.bottomLeft.y, observation.bottomRight.y]
    guard let minX = xs.min(), let maxX = xs.max(), let minY = ys.min(), let maxY = ys.max() else {
        return 0
    }
    return CGFloat((maxX - minX) * (maxY - minY))
}

func detectedRectangle(for image: CIImage) throws -> VNRectangleObservation? {
    let request = VNDetectRectanglesRequest()
    request.maximumObservations = 8
    request.minimumConfidence = manifest.minimumConfidence
    request.minimumSize = manifest.minimumSize
    request.minimumAspectRatio = 0.35
    request.maximumAspectRatio = 1.8
    request.quadratureTolerance = 30

    let handler = VNImageRequestHandler(ciImage: image, options: [:])
    try handler.perform([request])
    return request.results?.max { lhs, rhs in
        let leftScore = rectangleArea(lhs) * CGFloat(lhs.confidence)
        let rightScore = rectangleArea(rhs) * CGFloat(rhs.confidence)
        return leftScore < rightScore
    }
}

func perspectiveCorrect(_ image: CIImage, page: Int) throws -> (CIImage, String, Float?) {
    let extent = image.extent.integral
    let filter = CIFilter.perspectiveCorrection()
    filter.inputImage = image

    if let override = manifest.corners[String(page)] {
        if override.count != 8 {
            fatalError("--corners for page \(page) must have exactly 8 numeric values")
        }
        filter.topLeft = pointFromTop(override, 0, height: extent.height)
        filter.topRight = pointFromTop(override, 2, height: extent.height)
        filter.bottomLeft = pointFromTop(override, 4, height: extent.height)
        filter.bottomRight = pointFromTop(override, 6, height: extent.height)
        guard let output = filter.outputImage else { fatalError("Perspective correction failed for page \(page)") }
        return (output, "manual-corners", nil)
    }

    guard let observation = try detectedRectangle(for: image) else {
        return (image, "fallback-no-rectangle", nil)
    }
    filter.topLeft = normalizedPoint(observation.topLeft, extent: extent)
    filter.topRight = normalizedPoint(observation.topRight, extent: extent)
    filter.bottomLeft = normalizedPoint(observation.bottomLeft, extent: extent)
    filter.bottomRight = normalizedPoint(observation.bottomRight, extent: extent)
    guard let output = filter.outputImage else { fatalError("Perspective correction failed for page \(page)") }
    return (output, "auto-rectangle", observation.confidence)
}

func enhanceScan(_ image: CIImage) -> CIImage {
    var output = image
    let controls = CIFilter.colorControls()
    controls.inputImage = output
    controls.saturation = Float(manifest.saturation)
    controls.contrast = Float(manifest.contrast)
    controls.brightness = Float(manifest.brightness)
    if let adjusted = controls.outputImage { output = adjusted }

    let sharpen = CIFilter.sharpenLuminance()
    sharpen.inputImage = output
    sharpen.sharpness = Float(manifest.sharpness)
    if let sharpened = sharpen.outputImage { output = sharpened }
    return output
}

func makePDF(imagePaths: [String], outputPath: String) {
    var defaultMediaBox = CGRect(x: 0, y: 0, width: 1000, height: 1400)
    guard let consumer = CGDataConsumer(url: URL(fileURLWithPath: outputPath) as CFURL),
          let context = CGContext(consumer: consumer, mediaBox: &defaultMediaBox, nil) else {
        fatalError("Could not create PDF: \(outputPath)")
    }

    for path in imagePaths {
        guard let image = NSImage(contentsOfFile: path),
              let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            fatalError("Could not load PDF page image: \(path)")
        }
        var mediaBox = CGRect(x: 0, y: 0, width: cgImage.width, height: cgImage.height)
        let pageInfo = [kCGPDFContextMediaBox as String: NSData(bytes: &mediaBox, length: MemoryLayout<CGRect>.size)] as CFDictionary
        context.beginPDFPage(pageInfo)
        context.draw(cgImage, in: mediaBox)
        context.endPDFPage()
    }
    context.closePDF()
}

var rotatedPaths: [String] = []
var scannedPaths: [String] = []
var results: [PageResult] = []

for (index, inputPath) in manifest.inputs.enumerated() {
    let page = index + 1
    let inputURL = URL(fileURLWithPath: inputPath)
    guard let image = CIImage(contentsOf: inputURL) else {
        fatalError("Could not read image: \(inputPath)")
    }

    let rotation = normalizedRotation(manifest.rotations[index])
    let upright = orient(image, clockwise: rotation)
    let rotatedURL = rotatedDir.appendingPathComponent(String(format: "page-%03d.jpg", page))
    try writeJPEG(upright, to: rotatedURL)

    let (corrected, mode, confidence) = try perspectiveCorrect(upright, page: page)
    let scanned = enhanceScan(corrected)
    let scannedURL = scannedDir.appendingPathComponent(String(format: "page-%03d.jpg", page))
    try writeJPEG(scanned, to: scannedURL)

    rotatedPaths.append(rotatedURL.path)
    scannedPaths.append(scannedURL.path)
    results.append(PageResult(
        page: page,
        input: inputPath,
        rotation: rotation,
        rotatedImage: rotatedURL.path,
        scannedImage: scannedURL.path,
        scannerMode: mode,
        confidence: confidence
    ))
}

let rotatedPDF = outputDir.appendingPathComponent("\(manifest.basename)_rotated.pdf").path
let scannedPDF = outputDir.appendingPathComponent("\(manifest.basename)_scanned.pdf").path
makePDF(imagePaths: rotatedPaths, outputPath: rotatedPDF)
makePDF(imagePaths: scannedPaths, outputPath: scannedPDF)

let summary: [String: Any] = [
    "rotated_pdf": rotatedPDF,
    "scanned_pdf": scannedPDF,
    "pages": manifest.inputs.count,
    "results": results.map { result in
        [
            "page": result.page,
            "input": result.input,
            "rotation": result.rotation,
            "rotated_image": result.rotatedImage,
            "scanned_image": result.scannedImage,
            "scanner_mode": result.scannerMode,
            "confidence": result.confidence as Any
        ]
    }
]
let summaryData = try JSONSerialization.data(withJSONObject: summary, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(summaryData)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
'''


def natural_key(path: Path) -> list[object]:
    import re

    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def parse_corners(values: list[str]) -> dict[str, list[float]]:
    corners: dict[str, list[float]] = {}
    for raw in values:
        if ":" not in raw:
            raise SystemExit(f"--corners must be page:x1,y1,x2,y2,x3,y3,x4,y4, got: {raw}")
        page, coords = raw.split(":", 1)
        page = page.strip()
        if not page.isdigit() or int(page) < 1:
            raise SystemExit(f"--corners page must be a 1-based page number, got: {page}")
        try:
            numbers = [float(item.strip()) for item in coords.split(",") if item.strip()]
        except ValueError as exc:
            raise SystemExit(f"--corners contains a non-numeric coordinate: {raw}") from exc
        if len(numbers) != 8:
            raise SystemExit(f"--corners needs 8 coordinates, got {len(numbers)}: {raw}")
        corners[str(int(page))] = numbers
    return corners


def parse_rotations(rotation: int, rotations: str | None, count: int) -> list[int]:
    if rotations:
        values = [item.strip() for item in rotations.split(",") if item.strip()]
        if len(values) != count:
            raise SystemExit(f"--rotations has {len(values)} values but {count} images were provided")
        parsed = [int(value) for value in values]
    else:
        parsed = [rotation] * count
    bad = [value for value in parsed if value % 360 not in (0, 90, 180, 270)]
    if bad:
        raise SystemExit(f"Rotations must be 0, 90, 180, or 270 degrees clockwise: {bad}")
    return [value % 360 for value in parsed]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a rotation-only PDF and a scanner-style perspective-corrected PDF from page images."
    )
    parser.add_argument("images", nargs="+", help="Input page images in desired page order")
    parser.add_argument("--output-dir", required=True, help="Directory for PDFs and intermediate images")
    parser.add_argument("--basename", default="document", help="Base filename for output PDFs")
    parser.add_argument("--rotation", type=int, default=0, help="Clockwise rotation for all pages: 0, 90, 180, or 270")
    parser.add_argument("--rotations", help="Comma-separated per-page clockwise rotations, e.g. 90,90,270,0")
    parser.add_argument(
        "--corners",
        action="append",
        default=[],
        help="Manual page corners as page:tlx,tly,trx,try,blx,bly,brx,bry using rotated-image pixels with y from top",
    )
    parser.add_argument("--no-sort", action="store_true", help="Keep input paths exactly as provided")
    parser.add_argument("--quality", type=float, default=0.94, help="JPEG quality for intermediate page images")
    parser.add_argument("--saturation", type=float, default=0.92, help="Scanner PDF saturation adjustment")
    parser.add_argument("--contrast", type=float, default=1.08, help="Scanner PDF contrast adjustment")
    parser.add_argument("--brightness", type=float, default=0.015, help="Scanner PDF brightness adjustment")
    parser.add_argument("--sharpness", type=float, default=0.25, help="Scanner PDF luminance sharpening")
    parser.add_argument("--minimum-confidence", type=float, default=0.35, help="Vision rectangle minimum confidence")
    parser.add_argument("--minimum-size", type=float, default=0.35, help="Vision rectangle minimum size")
    args = parser.parse_args()

    swift = shutil.which("swift")
    if not swift:
        raise SystemExit("This script requires macOS Swift at /usr/bin/swift or in PATH")

    inputs = [Path(image).expanduser().resolve() for image in args.images]
    missing = [str(path) for path in inputs if not path.exists()]
    if missing:
        raise SystemExit("Input image(s) not found:\n" + "\n".join(missing))
    if not args.no_sort:
        inputs = sorted(inputs, key=natural_key)

    rotations = parse_rotations(args.rotation, args.rotations, len(inputs))
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "inputs": [str(path) for path in inputs],
        "rotations": rotations,
        "outputDir": str(output_dir),
        "basename": args.basename,
        "jpegQuality": args.quality,
        "saturation": args.saturation,
        "contrast": args.contrast,
        "brightness": args.brightness,
        "sharpness": args.sharpness,
        "minimumConfidence": args.minimum_confidence,
        "minimumSize": args.minimum_size,
        "corners": parse_corners(args.corners),
    }

    with tempfile.TemporaryDirectory(prefix="merge-images-into-pdf-") as temp_dir:
        temp_path = Path(temp_dir)
        manifest_path = temp_path / "manifest.json"
        swift_path = temp_path / "create_document_pdfs.swift"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        swift_path.write_text(SWIFT_SOURCE, encoding="utf-8")
        result = subprocess.run([swift, str(swift_path), str(manifest_path)], text=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
