FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// One half of a two-part casting mold (half a teardrop lure), printed parting face UP. The cavity, a pour channel
// and four alignment-pin holes are cut into the top (parting) face in ONE sketch + ONE cut. To save filament the
// block is cored out from the BOTTOM face with a grid of rectangular pockets (for loops) that leaves ribs between
// them. Two pocket depths, two sketches + two blind cuts:
//   shallow pockets (cells within `skin` of the cavity/channel footprint): height - cavityDepth - skin, so >= 3 mm of
//   solid is left under the cavity floor and beside it; deep pockets (all other cells): height - plate, leaving a
//   solid parting plate of 4 mm that the ribs stiffen.
// Pockets stay clear of the rim (>= 5 mm) and of the pin holes (>= 4 mm of material). Ribs are 2.4 mm (2 x the 1.2 mm
// minimum wall) and cells are <= 12 mm, so the pocket roofs bridge cleanly when the pockets face the bed.
// Pockets open from one face only (no closed voids).
export function build(context is Context, id is Id)
{
    variable(context, "length", 120 * millimeter);
    variable(context, "width", 60 * millimeter);
    variable(context, "height", 20 * millimeter);
    variable(context, "cavityDepth", 6 * millimeter);     // also the depth of the pin holes and the pour channel
    variable(context, "rib", 2.4 * millimeter);
    variable(context, "cell", 10 * millimeter);
    variable(context, "skin", 3 * millimeter);
    variable(context, "plate", 4 * millimeter);
    variable(context, "rim", 5 * millimeter);
    variable(context, "pinHole", 4 * millimeter);
    variable(context, "pinX", 52 * millimeter);
    variable(context, "pinY", 22 * millimeter);

    var base = newSketch(context, id + "Block sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(base, "outline", { "firstCorner" : vector(-#length / 2, -#width / 2), "secondCorner" : vector(#length / 2, #width / 2) });
    skSolve(base);
    extrude(context, id + "Block", { "entities" : qSketchRegion(id + "Block sketch"), "endBound" : BoundingType.BLIND, "depth" : #height });

    // cavity footprint, shared by the cavity sketch and the pocket zones: lure body x -25..38, y +-10; channel x 38..end, y +-3
    const lureX0 = -25 * millimeter;
    const lureX1 = 38 * millimeter;
    const lureHalf = 10 * millimeter;
    const chanHalf = 3 * millimeter;

    // parting face (top): half-lure cavity + pour channel out to the +X end as one profile, plus the pin holes.
    // The plane is free (no API call); the cut goes down into the block, so it is flipped.
    cPlane(context, id + "Parting plane", { "entities" : qCreatedBy(makeId("Top"), EntityType.FACE), "offset" : #height });
    var cav = newSketch(context, id + "Cavity sketch", { "sketchPlane" : qCreatedBy(id + "Parting plane", EntityType.FACE) });
    skArc(cav, "head", { "start" : vector(-15 * millimeter, -lureHalf), "mid" : vector(lureX0, 0 * millimeter),
        "end" : vector(-15 * millimeter, lureHalf) });
    skPolyline(cav, "tail", { "points" : [
        vector(-15 * millimeter, lureHalf), vector(lureX1, chanHalf),
        vector(#length / 2, chanHalf), vector(#length / 2, -chanHalf),
        vector(lureX1, -chanHalf), vector(-15 * millimeter, -lureHalf)] });
    for (var sx in [-1, 1])
    {
        for (var sy in [-1, 1])
        {
            skCircle(cav, "pin" ~ (sx > 0 ? "R" : "L") ~ (sy > 0 ? "B" : "F"), {
                "center" : vector(sx * #pinX, sy * #pinY), "radius" : #pinHole / 2 });
        }
    }
    skSolve(cav);
    extrude(context, id + "Cavity", {
        "entities" : qSketchRegion(id + "Cavity sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.BLIND,
        "depth" : #cavityDepth,
        "oppositeDirection" : true,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Block", EntityType.BODY) });

    // core-out from the bottom (the Top plane is the bottom face): a centred grid of cells that fits inside the rim,
    // skipping any cell closer than 4 mm to a pin hole. Cells near the cavity go in the shallow sketch, the rest in
    // the deep one.
    const nx = floor((#length - 2 * #rim + #rib) / (#cell + #rib));
    const ny = floor((#width - 2 * #rim + #rib) / (#cell + #rib));
    const keepOut = 4 * millimeter + #pinHole / 2;        // 4 mm of material around a pin hole
    var shallow = newSketch(context, id + "Shallow sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    var deep = newSketch(context, id + "Deep sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    for (var i = 0; i < nx; i += 1)
    {
        for (var j = 0; j < ny; j += 1)
        {
            const x0 = -(nx * #cell + (nx - 1) * #rib) / 2 + i * (#cell + #rib);
            const y0 = -(ny * #cell + (ny - 1) * #rib) / 2 + j * (#cell + #rib);
            const x1 = x0 + #cell;
            const y1 = y0 + #cell;
            // gap between the cell and the nearest pin centre (pins at +-pinX, +-pinY), per axis
            const dx = abs(abs(x0 + #cell / 2) - #pinX) - #cell / 2;
            const dy = abs(abs(y0 + #cell / 2) - #pinY) - #cell / 2;
            if (dx > keepOut || dy > keepOut || (dx > 0 * millimeter && dy > 0 * millimeter && dx * dx + dy * dy > keepOut * keepOut))
            {
                // within `skin` of the lure body or of the pour channel (bounding boxes)?
                const nearLure = x0 < lureX1 + #skin && x1 > lureX0 - #skin && y0 < lureHalf + #skin && y1 > -lureHalf - #skin;
                const nearChannel = x1 > lureX1 - #skin && y0 < chanHalf + #skin && y1 > -chanHalf - #skin;
                skRectangle(nearLure || nearChannel ? shallow : deep, "cell" ~ i ~ "_" ~ j, {
                    "firstCorner" : vector(x0, y0), "secondCorner" : vector(x1, y1) });
            }
        }
    }
    skSolve(shallow);
    skSolve(deep);
    extrude(context, id + "Shallow pockets", {
        "entities" : qSketchRegion(id + "Shallow sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.BLIND,
        "depth" : #height - #cavityDepth - #skin,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Block", EntityType.BODY) });
    extrude(context, id + "Deep pockets", {
        "entities" : qSketchRegion(id + "Deep sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.BLIND,
        "depth" : #height - #plate,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Block", EntityType.BODY) });
}
