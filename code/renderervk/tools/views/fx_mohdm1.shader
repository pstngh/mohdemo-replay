// Installed for fx_mohdm1.txt only (as main/scripts/aaa_fx_mohdm1.shader:
// the first shader file wins). Three dm/mohdm1 world shaders replaced by
// stages that the effects shader draws (shaders/effects.vert): every deform
// it takes (wave with and without a frequency, move, bulge, flap s and t),
// tcGen environment, environmentmodel and vector, the tcMods (turb,
// scroll, scale, stretch, transform, rotate, offset, parallax, wavetrans,
// wavetrant, bulge, which changes nothing), a lightmap stage with a tcMod,
// alphaGen distFade and oneMinusDistFade on a constant color (rgbGen wave,
// rgbGen const; the trees of the game's maps have distFade on the vertex
// colors). The build before, and r_staticEffects 0, draw them on the CPU
// (tess).

// the ground (the te_* views of mohdm1_texenv)
textures/mohcommon/gravelrd
{
	surfaceparm dirt
	deformVertexes wave 100 sin 0 3 0 0.5
	deformVertexes move 0 0 1 sin 0 2 0 0.3
	{
		map textures/mohcommon/gravelrd.tga
		rgbGen identity
		tcMod turb 0 0.05 0 0.2
		tcMod scroll 0.1 0.05
	}
	{
		map $lightmap
		rgbGen identity
		blendFunc GL_DST_COLOR GL_ZERO
		tcMod scale 1 1
	}
	{
		map textures/mohcommon/environ_puddle.tga
		blendFunc blend
		rgbGen wave sin 0.5 0.25 0 0.75
		alphaGen distFade 200 800
		tcMod rotate 20
		tcMod stretch sin 1 0.2 0 0.5
	}
}

// the walls
textures/mohtest/platestonedrktn
{
	surfaceparm stone
	deformVertexes bulge 3 2 1
	deformVertexes wave 10 sin 2 0 0 0
	deformVertexes flap t 10 sin 0 1 0 0.4 0 1
	{
		map textures/mohtest/platestonedrktn.tga
		rgbGen identity
		tcGen vector 0.01 0 0.003 0 0.01 0.002
		tcMod transform 1 0.2 0.1 1 0.3 0.1
		tcMod offset 0.25 0.5
	}
	{
		map textures/mohcommon/environ_puddle.tga
		tcGen environment
		blendFunc add
		rgbGen identity
	}
	{
		map $lightmap
		rgbGen identity
		blendFunc GL_DST_COLOR GL_ZERO
	}
	{
		map textures/mohcommon/gravelrd.tga
		blendFunc blend
		rgbGen const 0.9 0.5 0.3
		alphaGen oneMinusDistFade 100 700
	}
}

// the street
textures/mohtest/brickstreet1
{
	deformVertexes flap s 10 sin 0 2 0 0.4 0 1
	{
		map textures/mohtest/brickstreet1.tga
		rgbGen identity
	nextBundle
		map $lightmap
	}
	{
		map textures/mohcommon/environ_puddle.tga
		tcGen environmentmodel
		tcMod parallax 0.001 0.002
		tcMod wavetrans sin 0 0.1 0 0.3
		tcMod wavetrant sin 0 0.1 0 0.3
		tcMod bulge 1 2 3 4
		blendFunc add
		rgbGen identity
	}
}
