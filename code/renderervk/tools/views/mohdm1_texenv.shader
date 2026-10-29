// Installed by compare.sh for mohdm1_texenv.txt only (as
// main/scripts/aaa_mohdm1_texenv.shader: the first shader file wins). No
// shipped shader produces TEXENV_ADD, so these replace two dm/mohdm1 shaders.

// an opaque stage and an additive stage, both with identity colors: one
// TEXENV_ADD stage with r_ext_texture_env_add 1, two passes with 0. (A
// lightmap in one of the two would send the collapsed stage to
// RB_StageIteratorLightmappedMultitextureUnfogged, which always modulates.)
textures/mohcommon/gravelrd
{
	surfaceparm dirt
	{
		map textures/mohcommon/gravelrd.tga
		rgbGen identity
	}
	{
		map textures/mohcommon/gravelrd.tga
		blendFunc add
		rgbGen identity
		tcMod scale 0.37 0.37
	}
	{
		map $lightmap
		rgbGen identity
		blendFunc GL_DST_COLOR GL_ZERO
	}
}

// nextBundle add: TEXENV_ADD whatever r_ext_texture_env_add
textures/mohtest/platestonedrktn
{
	surfaceparm stone
	{
		map textures/mohtest/platestonedrktn.tga
		rgbGen identity
	nextBundle add
		map textures/mohcommon/gravelrd.tga
		tcMod scale 0.37 0.37
	}
	{
		map $lightmap
		rgbGen identity
		blendFunc GL_DST_COLOR GL_ZERO
		depthFunc equal
	}
}
