FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Multicolor tag: one solid body per color (base, disc, bar), each named and colored with setProperty.
// The raised art sits on the base top face (bodies touch, never overlap). Same name = same filament.
export function build(context is Context, id is Id)
{
    variable(context, "width", 40 * millimeter);
    variable(context, "depth", 20 * millimeter);
    variable(context, "baseThickness", 3 * millimeter);
    variable(context, "artHeight", 1.2 * millimeter);

    var base = newSketch(context, id + "Base sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(base, "outline", { "firstCorner" : vector(-#width / 2, -#depth / 2), "secondCorner" : vector(#width / 2, #depth / 2) });
    skSolve(base);
    extrude(context, id + "Base", {
        "entities" : qSketchRegion(id + "Base sketch"),
        "operationType" : NewBodyOperationType.NEW,
        "endBound" : BoundingType.BLIND,
        "depth" : #baseThickness });

    // Art sketches on the base top face (z = baseThickness): a disc and a bar.
    var disc = newSketch(context, id + "Disc sketch", { "sketchPlane" : qCapEntity(id + "Base", CapType.END, EntityType.FACE) });
    skCircle(disc, "disc", { "center" : vector(-10 * millimeter, 0 * millimeter), "radius" : 6 * millimeter });
    skSolve(disc);
    extrude(context, id + "Disc", {
        "entities" : qSketchRegion(id + "Disc sketch"),
        "operationType" : NewBodyOperationType.NEW,
        "endBound" : BoundingType.BLIND,
        "depth" : #artHeight });

    var bar = newSketch(context, id + "Bar sketch", { "sketchPlane" : qCapEntity(id + "Base", CapType.END, EntityType.FACE) });
    skRectangle(bar, "bar", { "firstCorner" : vector(2 * millimeter, -3 * millimeter), "secondCorner" : vector(16 * millimeter, 3 * millimeter) });
    skSolve(bar);
    extrude(context, id + "Bar", {
        "entities" : qSketchRegion(id + "Bar sketch"),
        "operationType" : NewBodyOperationType.NEW,
        "endBound" : BoundingType.BLIND,
        "depth" : #artHeight });

    // Names and colors (components 0..1).
    setProperty(context, { "entities" : qCreatedBy(id + "Base", EntityType.BODY), "propertyType" : PropertyType.NAME, "value" : "Base" });
    setProperty(context, { "entities" : qCreatedBy(id + "Base", EntityType.BODY), "propertyType" : PropertyType.APPEARANCE, "value" : color(0.1, 0.1, 0.1) });
    setProperty(context, { "entities" : qCreatedBy(id + "Disc", EntityType.BODY), "propertyType" : PropertyType.NAME, "value" : "Yellow" });
    setProperty(context, { "entities" : qCreatedBy(id + "Disc", EntityType.BODY), "propertyType" : PropertyType.APPEARANCE, "value" : color(1, 0.8, 0) });
    setProperty(context, { "entities" : qCreatedBy(id + "Bar", EntityType.BODY), "propertyType" : PropertyType.NAME, "value" : "White" });
    setProperty(context, { "entities" : qCreatedBy(id + "Bar", EntityType.BODY), "propertyType" : PropertyType.APPEARANCE, "value" : color(0.95, 0.95, 0.95) });
}
