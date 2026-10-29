// Installed by compare.sh for obj_team3_deepbluesea.txt only. The open-sea
// shader textures/misc_outside/deepbluesea is only used by m3l1a, where the
// camera shakes with the random artillery, so its stages (copied from
// scripts/misc_outside.shader, editor keywords left out) replace the sea of
// obj/obj_team3 to compare them deterministically.
textures/misc_outside/deepbluesea_runup
{
	surfaceparm trans
	surfaceparm water
	surfaceparm nolightmap
	cull none
	deformvertexes flap t 10 sin 0 10 0 .10 0 10
	{
		nopicmip
		map textures/misc_outside/oceandday1.tga
		rgbGen identityLighting
		tcMod scale 16 22
		tcMod scroll 0.01 .03
	nextbundle
		map textures/misc_outside/oceandday1.tga
		tcMod scale -16 22
		tcMod scroll 0.01 0.04
	}
	{
		nopicmip
		map textures/misc_outside/oceandday1.tga
		blendFunc add
		tcMod scale .2 .5
		tcMod scroll 0 .005
	nextbundle
		map textures/misc_outside/oceandday1.tga
		tcMod scale .2 .5
		tcMod scroll 0 .01
	}
}
