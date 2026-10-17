-- plate_client_kit.ms : PLATE client packet kit, version 0.1.0
-- Exports one camera next to a client plate: a multilayer EXR with Z depth, normals, Cryptomatte
-- (object and material), wire colour and diffuse filter; the camera as JSON; one legend line per
-- visible node. Local files only: no network, no scripting bridges, no shell. ASCII, CRLF.
-- Run with: fileIn "plate_client_kit.ms". Property names marked (unverified) are the best known
-- V-Ray names, not yet checked on a Max box: a miss is listed under "warnings" in the camera JSON
-- and in the Listener, and the property list of the element is printed so the name can be fixed.
(
    local kitVersion = "0.1.0"
    local kitWarnings = #()
    local kitOkChars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    local kitTitle = "PLATE Packet Kit"

    fn kitWarn msg =
    (
        append kitWarnings msg
        format "PLATE kit: %\n" msg
    )

    -- file-name stem of a camera name; plate_max.kit_ingest.kit_stem does the same replacement
    fn kitStem s =
    (
        local r = ""
        for i = 1 to s.count do
            r += (if (findString kitOkChars (s[i])) != undefined then (s[i]) else "_")
        r
    )

    fn kitNum v =
    (
        local s = "null"
        if v != undefined do
        (
            local t = try (formattedPrint (v as float) format:".10g") catch (v as string)
            s = substituteString t "," "."
            if (findString s "n") != undefined do s = "null"
        )
        s
    )

    fn kitBool v = (if v == true then "true" else "false")

    fn kitStr v =
    (
        local r = v as string
        r = substituteString r "\\" "\\\\"
        r = substituteString r "\"" "\\\""
        r = substituteString r "\r" "\\r"
        r = substituteString r "\n" "\\n"
        r = substituteString r "\t" "\\t"
        "\"" + r + "\""
    )

    -- legend fields are percent-encoded so that "|" and line breaks in names survive
    fn kitEsc v =
    (
        local r = v as string
        r = substituteString r "%" "%25"
        r = substituteString r "|" "%7C"
        r = substituteString r "\r" "%0D"
        substituteString r "\n" "%0A"
    )

    fn kitGet obj names dflt =
    (
        local v = dflt
        local found = false
        for n in names while not found do
            if isProperty obj n do ( v = getProperty obj n; found = true )
        v
    )

    fn kitSet obj names val label =
    (
        local done = false
        for n in names while not done do
            if isProperty obj n do ( setProperty obj n val; done = true )
        if not done do
        (
            kitWarn (label + ": none of " + (names as string) + " exists")
            kitWarn (label + ": properties are " + ((getPropNames obj) as string))
        )
        done
    )

    fn kitElementClass nm =
    (
        local found = undefined
        for c in RenderElement.classes while found == undefined do
            if (c as string) == nm do found = c
        found
    )

    fn kitConfigure e nm =
    (
        if nm == "VRayZDepth" do
        (
            kitSet e #(#zdepth_clamp, #clamp, #clamped) false "zdepth unclamped (unverified)"
            kitSet e #(#filterOn, #filter_on, #filtering, #filter) false "zdepth unfiltered (unverified)"
        )
        if nm == "CryptoObject" or nm == "CryptoMaterial" do
        (
            kitSet e #(#levels, #cryptomatte_levels, #rank_levels, #depth) 6 (nm + " levels (unverified)")
            local kind = if nm == "CryptoObject" then 0 else 1
            kitSet e #(#id_type, #idType, #cryptomatte_type, #type, #mode) kind (nm + " id type (unverified)")
        )
    )

    -- V-Ray lens shift, tilt and distortion are not in packet v0.1: refuse before the render
    fn kitAbs v = (try (abs v) catch (0.0))

    fn kitLensProblem cam =
    (
        local sh = 0.0
        sh += (kitAbs (kitGet cam #(#horizontal_shift, #horizontal_offset) 0.0))
        sh += (kitAbs (kitGet cam #(#vertical_shift, #vertical_offset) 0.0))
        sh += (kitAbs (kitGet cam #(#vertical_tilt) 0.0))
        sh += (kitAbs (kitGet cam #(#horizontal_tilt) 0.0))
        sh += (kitAbs (kitGet cam #(#distortion) 0.0))
        local auto = kitGet cam #(#auto_vertical_tilt, #guess_vertical_tilt) false
        if sh > 0.0 or auto == true then "shift, tilt or distortion is set" else ""
    )

    fn kitColour =
    (
        local raw = try ((IDisplayGamma.colorCorrectionMode) as string) catch ("unknown")
        local gam = try (IDisplayGamma.gamma) catch (undefined)
        local low = toLower raw
        local mode = "unknown"
        if (findString low "ocio") != undefined do mode = "ocio"
        if (findString low "gamma") != undefined do mode = "legacy_gamma"
        #(mode, raw, gam)
    )

    fn kitRow p = ("[" + (kitNum p.x) + ", " + (kitNum p.y) + ", " + (kitNum p.z) + "]")

    fn kitCameraJson cam exrName legName elapsed =
    (
        local tm = cam.objecttransform
        local film = kitGet cam #(#film_width) 36.0
        local zoom = kitGet cam #(#zoom_factor) 1.0
        local fov = kitGet cam #(#fov) undefined
        local focal = kitGet cam #(#focal_length, #focal_length_mm, #lens) undefined
        if focal == undefined and fov != undefined do focal = (film / 2.0) / (tan (fov / 2.0))
        if fov == undefined and focal != undefined do fov = 2.0 * (atan (film / (2.0 * focal * zoom)))
        local fnum = kitGet cam #(#f_number) undefined
        local focus = kitGet cam #(#focus_distance, #targetDistance) undefined
        local shutter = kitGet cam #(#shutter_speed) undefined
        local cm = kitColour()
        local ver = maxVersion()
        local vtxt = ""
        for i = 1 to ver.count do vtxt += (if i > 1 then ", " else "") + ((ver[i]) as string)
        local wtxt = ""
        for i = 1 to kitWarnings.count do wtxt += (if i > 1 then ", " else "") + (kitStr (kitWarnings[i]))
        local b = "{\n"
        b += "  \"kit\": \"plate_client_kit\",\n  \"kit_version\": " + (kitStr kitVersion) + ",\n"
        b += "  \"camera\": " + (kitStr cam.name) + ",\n  \"camera_class\": " + (kitStr ((classOf cam) as string)) + ",\n"
        b += "  \"transform\": [" + (kitRow tm.row1) + ", " + (kitRow tm.row2) + ", " + (kitRow tm.row3) + ", " + (kitRow tm.row4) + "],\n"
        b += "  \"fov\": " + (kitNum fov) + ",\n  \"focal_length_mm\": " + (kitNum focal) + ",\n"
        b += "  \"film_width\": " + (kitNum film) + ",\n  \"zoom_factor\": " + (kitNum zoom) + ",\n"
        b += "  \"render_width\": " + (renderWidth as string) + ",\n  \"render_height\": " + (renderHeight as string) + ",\n"
        b += "  \"pixel_aspect\": " + (kitNum renderPixelAspect) + ",\n"
        b += "  \"frame\": " + ((currentTime.frame as integer) as string) + ",\n  \"fps\": " + (kitNum frameRate) + ",\n"
        b += "  \"units\": {\"system_type\": " + (kitStr ((units.SystemType) as string)) + ", \"system_scale\": " + (kitNum units.SystemScale) + "},\n"
        b += "  \"renderer\": " + (kitStr ((classOf renderers.current) as string)) + ",\n"
        b += "  \"max_version\": [" + vtxt + "],\n"
        b += "  \"colour_mode\": " + (kitStr (cm[1])) + ",\n  \"colour_mode_raw\": " + (kitStr (cm[2])) + ",\n  \"gamma\": " + (kitNum (cm[3])) + ",\n"
        b += "  \"f_number\": " + (kitNum fnum) + ",\n  \"focus_distance\": " + (kitNum focus) + ",\n"
        b += "  \"dof_enabled\": " + (kitBool (kitGet cam #(#use_dof, #dof_enabled) false)) + ",\n"
        b += "  \"iso\": " + (kitNum (kitGet cam #(#ISO) undefined)) + ",\n  \"shutter_speed\": " + (kitNum shutter) + ",\n"
        b += "  \"lens_shift\": {\"horizontal\": " + (kitNum (kitGet cam #(#horizontal_shift, #horizontal_offset) 0.0)) + ", \"vertical\": " + (kitNum (kitGet cam #(#vertical_shift, #vertical_offset) 0.0)) + "},\n"
        b += "  \"tilt\": {\"vertical\": " + (kitNum (kitGet cam #(#vertical_tilt) 0.0)) + ", \"horizontal\": " + (kitNum (kitGet cam #(#horizontal_tilt) 0.0)) + ", \"auto\": " + (kitBool (kitGet cam #(#auto_vertical_tilt, #guess_vertical_tilt) false)) + "},\n"
        b += "  \"distortion\": " + (kitNum (kitGet cam #(#distortion) 0.0)) + ",\n"
        b += "  \"render_time_s\": " + (kitNum elapsed) + ",\n"
        b += "  \"exr\": " + (kitStr exrName) + ",\n  \"legend\": " + (kitStr legName) + ",\n"
        b += "  \"elements\": {\"beauty\": \"RGBA\", \"zdepth\": \"VRayZDepth\", \"normals\": \"VRayNormals\", \"cryptomatte_object\": \"CryptoObject\", \"cryptomatte_material\": \"CryptoMaterial\", \"wire_colour\": \"VRayWireColor\", \"diffuse_filter\": \"VRayDiffuseFilter\"},\n"
        b += "  \"warnings\": [" + wtxt + "]\n}\n"
        b
    )

    -- one node|layer|material|wirecolor line per visible node
    fn kitLegendText =
    (
        local ss = StringStream ""
        for o in geometry where (not o.isHidden) and o.renderable and (not o.layer.ishidden) do
        (
            local mat = if o.material != undefined then o.material.name else ""
            local wc = o.wirecolor
            format "%|%|%|%,%,%\n" (kitEsc o.name) (kitEsc o.layer.name) (kitEsc mat) (wc.r as integer) (wc.g as integer) (wc.b as integer) to:ss
        )
        ss as string
    )

    fn kitWriteText path text =
    (
        local f = try (createFile path encoding:#utf8) catch (undefined)
        if f == undefined do f = createFile path
        local ok = false
        if f != undefined do ( format "%" text to:f; close f; ok = true )
        ok
    )

    fn kitRestore mgr added saved vr oldRaw oldName oldSplit oldActive =
    (
        for e in added do mgr.RemoveRenderElement e
        mgr.SetElementsActive oldActive
        for s in saved do s[1].enabled = s[2]
        kitSet vr #(#output_saveRawImage) oldRaw "restore raw image flag"
        kitSet vr #(#output_rawFileName) oldName "restore raw image file"
        kitSet vr #(#output_splitgbuffer) oldSplit "restore split channels flag"
    )

    -- the export; returns a status line for the dialog
    fn kitExport cam folder =
    (
        kitWarnings.count = 0
        local vr = renderers.current
        if cam == undefined then return "Pick a camera first."
        if not (matchPattern ((classOf vr) as string) pattern:"V_Ray*") then
        (
            messageBox "PLATE Packet Kit needs V-Ray as the current renderer." title:kitTitle
            return "Stopped: the renderer is not V-Ray."
        )
        if folder == "" then return "Pick an output folder first."
        makeDir folder all:true
        if not (doesFileExist folder) then return "Cannot create the output folder."
        local lens = kitLensProblem cam
        if lens != "" then
        (
            messageBox ("Packet v0.1 supports unshifted, untilted, undistorted cameras only: " + lens + ".") title:kitTitle
            return "Stopped: unsupported lens settings."
        )
        local stem = kitStem cam.name
        local exrName = stem + "_packet.exr"
        local legName = stem + "_legend.txt"
        local exrPath = pathConfig.appendPath folder exrName
        local mgr = maxOps.GetCurRenderElementMgr()
        local saved = #()
        local added = #()
        local oldActive = mgr.GetElementsActive()
        mgr.SetElementsActive true
        for i = 0 to (mgr.NumRenderElements() - 1) do
        (
            local e = mgr.GetRenderElement i
            append saved #(e, e.enabled)
            e.enabled = false
        )
        local specs = #(#("VRayZDepth", "VRayZDepth"), #("VRayNormals", "VRayNormals"), #("VRayCryptomatte", "CryptoObject"), #("VRayCryptomatte", "CryptoMaterial"), #("VRayWireColor", "VRayWireColor"), #("VRayDiffuseFilter", "VRayDiffuseFilter"))
        for s in specs do
        (
            local c = kitElementClass s[1]
            if c == undefined then kitWarn ("render element class not found: " + s[1]) else
            (
                local e = c()
                e.elementname = s[2]
                mgr.AddRenderElement e
                append added e
                kitConfigure e s[2]
            )
        )
        local oldRaw = kitGet vr #(#output_saveRawImage) false
        local oldName = kitGet vr #(#output_rawFileName) ""
        local oldSplit = kitGet vr #(#output_splitgbuffer) false
        local rawOk = kitSet vr #(#output_saveRawImage) true "save raw image"
        rawOk = (kitSet vr #(#output_rawFileName) exrPath "raw image file") and rawOk
        kitSet vr #(#output_splitgbuffer) false "split channels off"
        local status = ""
        local t0 = timestamp()
        if added.count == specs.count and rawOk then
        (
            try
            (
                local bm = render camera:cam vfb:false
                if bm != undefined do ( close bm; free bm )
            )
            catch ( status = "Render failed: " + (getCurrentException()) )
        )
        else status = "Stopped: a render element or the V-Ray raw output could not be set up (see the Listener)."
        local elapsed = (timestamp() - t0) / 1000.0
        kitRestore mgr added saved vr oldRaw oldName oldSplit oldActive
        if status == "" and not (doesFileExist exrPath) then status = "V-Ray wrote no EXR: " + exrPath
        if status == "" then
        (
            local okJ = kitWriteText (pathConfig.appendPath folder (stem + "_camera.json")) (kitCameraJson cam exrName legName elapsed)
            local okL = kitWriteText (pathConfig.appendPath folder legName) (kitLegendText())
            status = if okJ and okL then ("Exported " + stem + " to " + folder) else "Could not write the camera JSON or the legend."
        )
        status
    )

    rollout kitRollout "PLATE Packet Kit" width:380
    (
        label lblHelp "Exports Z, Cryptomatte and the camera next to a client plate." align:#left
        dropdownList ddCam "Camera" items:#() width:360
        edittext edOut "Folder" fieldWidth:300 text:""
        button btnPick "Pick folder..." width:110 across:2
        button btnExport "Export" width:110
        label lblStatus "" align:#left width:360 height:32

        on kitRollout open do
        (
            local names = for c in cameras where (superClassOf c == camera) collect c.name
            ddCam.items = names
            local active = viewport.getCamera()
            if active != undefined do
            (
                local pos = findItem names active.name
                if pos > 0 do ddCam.selection = pos
            )
        )
        on btnPick pressed do
        (
            local d = getSavePath caption:"PLATE kit output folder"
            if d != undefined do edOut.text = d
        )
        on btnExport pressed do
        (
            local cam = if ddCam.selected != undefined then (getNodeByName ddCam.selected exact:true) else undefined
            lblStatus.caption = kitExport cam edOut.text
        )
    )

    createDialog kitRollout
)
