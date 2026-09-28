#include "pch.h"
#include <sddl.h>
#pragma comment(lib, "advapi32.lib")
#include "Undocumented.h"
#include "Tools.h"
#include "EnumNames.h"
#include "MFTools.h"
#include "FrameGenerator.h"

HRESULT FrameGenerator::EnsureRenderTarget(UINT width, UINT height)
{
	if (!HasD3DManager())
	{
		// create a D2D1 render target from WIC bitmap
		wil::com_ptr_nothrow<ID2D1Factory> d2d1Factory;
		RETURN_IF_FAILED(D2D1CreateFactory(D2D1_FACTORY_TYPE_MULTI_THREADED, IID_PPV_ARGS(&d2d1Factory)));

		wil::com_ptr_nothrow<IWICImagingFactory> wicFactory;
		RETURN_IF_FAILED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_ALL, IID_PPV_ARGS(&wicFactory)));

		RETURN_IF_FAILED(wicFactory->CreateBitmap(width, height, GUID_WICPixelFormat32bppPBGRA, WICBitmapCacheOnDemand, &_bitmap));

		D2D1_RENDER_TARGET_PROPERTIES props{};
		props.pixelFormat.format = DXGI_FORMAT_B8G8R8A8_UNORM;
		props.pixelFormat.alphaMode = D2D1_ALPHA_MODE_PREMULTIPLIED;
		_frameBitmap.reset(); // bitmap pertence ao render target antigo
		RETURN_IF_FAILED(d2d1Factory->CreateWicBitmapRenderTarget(_bitmap.get(), props, &_renderTarget));

		RETURN_IF_FAILED(CreateRenderTargetResources(width, height));
	}

	_prevTime = MFGetSystemTime();
	_frame = 0;
	return S_OK;
}

const bool FrameGenerator::HasD3DManager() const
{
	return _texture != nullptr;
}

HRESULT FrameGenerator::SetD3DManager(IUnknown* manager, UINT width, UINT height)
{
	RETURN_HR_IF_NULL(E_POINTER, manager);
	RETURN_HR_IF(E_INVALIDARG, !width || !height);

	RETURN_IF_FAILED(manager->QueryInterface(&_dxgiManager));
	RETURN_IF_FAILED(_dxgiManager->OpenDeviceHandle(&_deviceHandle));

	wil::com_ptr_nothrow<ID3D11Device> device;
	RETURN_IF_FAILED(_dxgiManager->GetVideoService(_deviceHandle, IID_PPV_ARGS(&device)));

	// create a texture/surface to write
	CD3D11_TEXTURE2D_DESC desc
	(
		DXGI_FORMAT_B8G8R8A8_UNORM,
		width,
		height,
		1,
		1,
		D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET
	);
	RETURN_IF_FAILED(device->CreateTexture2D(&desc, nullptr, &_texture));
	wil::com_ptr_nothrow<IDXGISurface> surface;
	RETURN_IF_FAILED(_texture.copy_to(&surface));

	// create a D2D1 render target from 2D GPU surface
	wil::com_ptr_nothrow<ID2D1Factory> d2d1Factory;
	RETURN_IF_FAILED(D2D1CreateFactory(D2D1_FACTORY_TYPE_MULTI_THREADED, IID_PPV_ARGS(&d2d1Factory)));

	auto props = D2D1::RenderTargetProperties
	(
		D2D1_RENDER_TARGET_TYPE_DEFAULT,
		D2D1::PixelFormat(DXGI_FORMAT_UNKNOWN, D2D1_ALPHA_MODE_PREMULTIPLIED)
	);
	_frameBitmap.reset(); // bitmap pertence ao render target antigo
	RETURN_IF_FAILED(d2d1Factory->CreateDxgiSurfaceRenderTarget(surface.get(), props, &_renderTarget));

	RETURN_IF_FAILED(CreateRenderTargetResources(width, height));

	// create GPU RGB => NV12 converter
	RETURN_IF_FAILED(CoCreateInstance(CLSID_VideoProcessorMFT, nullptr, CLSCTX_ALL, IID_PPV_ARGS(&_converter)));

	wil::com_ptr_nothrow<IMFAttributes> atts;
	RETURN_IF_FAILED(_converter->GetAttributes(&atts));
	TraceMFAttributes(atts.get(), L"VideoProcessorMFT");

	MFT_OUTPUT_STREAM_INFO info{};
	RETURN_IF_FAILED(_converter->GetOutputStreamInfo(0, &info));
	WINTRACE(L"FrameGenerator::SetD3DManager CLSID_VideoProcessorMFT flags:0x%08X size:%u alignment:%u", info.dwFlags, info.cbSize, info.cbAlignment);

	wil::com_ptr_nothrow<IMFMediaType> inputType;
	RETURN_IF_FAILED(MFCreateMediaType(&inputType));
	inputType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
	inputType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_RGB32);
	MFSetAttributeSize(inputType.get(), MF_MT_FRAME_SIZE, width, height);
	RETURN_IF_FAILED(_converter->SetInputType(0, inputType.get(), 0));

	wil::com_ptr_nothrow<IMFMediaType> outputType;
	RETURN_IF_FAILED(MFCreateMediaType(&outputType));
	outputType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
	outputType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
	MFSetAttributeSize(outputType.get(), MF_MT_FRAME_SIZE, width, height);
	RETURN_IF_FAILED(_converter->SetOutputType(0, outputType.get(), 0));

	// make sure the video processor works on GPU
	RETURN_IF_FAILED(_converter->ProcessMessage(MFT_MESSAGE_SET_D3D_MANAGER, (ULONG_PTR)manager));
	return S_OK;
}

// common to CPU & GPU
HRESULT FrameGenerator::CreateRenderTargetResources(UINT width, UINT height)
{
	assert(_renderTarget);
	RETURN_IF_FAILED(_renderTarget->CreateSolidColorBrush(D2D1::ColorF(1, 1, 1, 1), &_whiteBrush));

	RETURN_IF_FAILED(DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, __uuidof(IDWriteFactory), (IUnknown**)&_dwrite));
	RETURN_IF_FAILED(_dwrite->CreateTextFormat(L"Segoe UI", nullptr, DWRITE_FONT_WEIGHT_NORMAL, DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_STRETCH_NORMAL, 40, L"", &_textFormat));
	RETURN_IF_FAILED(_textFormat->SetParagraphAlignment(DWRITE_PARAGRAPH_ALIGNMENT_CENTER));
	RETURN_IF_FAILED(_textFormat->SetTextAlignment(DWRITE_TEXT_ALIGNMENT_CENTER));
	_width = width;
	_height = height;
	return S_OK;
}

HRESULT FrameGenerator::Generate(IMFSample* sample, REFGUID format, IMFSample** outSample)
{
	RETURN_HR_IF_NULL(E_POINTER, sample);
	RETURN_HR_IF_NULL(E_POINTER, outSample);
	*outSample = nullptr;

	// desenha o quadro que o KONECTA mandou; sem ele, um aviso em vez de preto
	if (_renderTarget && _textFormat && _dwrite && _whiteBrush)
	{
		_renderTarget->BeginDraw();
		if (!DrawSharedFrame())
		{
			// Quem está na chamada vê que a câmera está ligada mas o app não:
			// mais claro que uma tela preta ou uma imagem congelada.
			_renderTarget->Clear(D2D1::ColorF(0.06f, 0.09f, 0.16f, 1));
			const wchar_t text[] = L"KONECTA\nAguardando imagem do app";
			wil::com_ptr_nothrow<IDWriteTextLayout> layout;
			if (SUCCEEDED(_dwrite->CreateTextLayout(text, (UINT32)wcslen(text), _textFormat.get(), (FLOAT)_width, (FLOAT)_height, &layout)))
			{
				_renderTarget->DrawTextLayout(D2D1::Point2F(0, 0), layout.get(), _whiteBrush.get());
			}
		}
		_renderTarget->EndDraw();
	}

	// build a sample using either D3D/DXGI (GPU) or WIC (CPU)
	wil::com_ptr_nothrow<IMFMediaBuffer> mediaBuffer;
	if (HasD3DManager())
	{
		// remove all existing buffers
		RETURN_IF_FAILED(sample->RemoveAllBuffers());

		// create a buffer from this and add to sample
		RETURN_IF_FAILED(MFCreateDXGISurfaceBuffer(__uuidof(ID3D11Texture2D), _texture.get(), 0, 0, &mediaBuffer));
		RETURN_IF_FAILED(sample->AddBuffer(mediaBuffer.get()));

		// if we're on GPU & format is not RGB, convert using GPU
		if (format == MFVideoFormat_NV12)
		{
			assert(_converter);
			RETURN_IF_FAILED(_converter->ProcessInput(0, sample, 0));

			// let converter build the sample for us, note it works because we gave it the D3DManager
			MFT_OUTPUT_DATA_BUFFER buffer = {};
			DWORD status = 0;
			RETURN_IF_FAILED(_converter->ProcessOutput(0, 1, &buffer, &status));
			*outSample = buffer.pSample;
		}
		else
		{
			sample->AddRef();
			*outSample = sample;
		}

		_frame++;
		return S_OK;
	}

	RETURN_IF_FAILED(sample->GetBufferByIndex(0, &mediaBuffer));
	wil::com_ptr_nothrow<IMF2DBuffer2> buffer2D;
	BYTE* scanline;
	LONG pitch;
	BYTE* start;
	DWORD length;
	RETURN_IF_FAILED(mediaBuffer->QueryInterface(IID_PPV_ARGS(&buffer2D)));
	RETURN_IF_FAILED(buffer2D->Lock2DSize(MF2DBuffer_LockFlags_Write, &scanline, &pitch, &start, &length));

	wil::com_ptr_nothrow<IWICBitmapLock> lock;
	auto hr = _bitmap->Lock(nullptr, WICBitmapLockRead, &lock);
	// now we're using regular COM macros because we want to be sure to unlock (or we could use try/catch)
	if (SUCCEEDED(hr))
	{
		UINT w, h;
		hr = lock->GetSize(&w, &h);
		if (SUCCEEDED(hr))
		{
			UINT wicStride;
			hr = lock->GetStride(&wicStride);
			if (SUCCEEDED(hr))
			{
				UINT wicSize;
				WICInProcPointer wicPointer;
				hr = lock->GetDataPointer(&wicSize, &wicPointer);
				if (SUCCEEDED(hr))
				{
					WINTRACE(L"WIC stride:%u WIC size:%u MF pitch:%u MF length:%u frame:%u format:%s", wicStride, wicSize, pitch, length, _frame, GUID_ToStringW(format).c_str());
					if (format == MFVideoFormat_NV12)
					{
						// note we could use MF's converter too
						hr = RGB32ToNV12(wicPointer, wicSize, wicStride, w, h, scanline, length, pitch);
					}
					else
					{
						hr = (wicSize != length || wicStride != pitch) ? E_FAIL : S_OK;
						if (SUCCEEDED(hr))
						{
							if (assert_true(wicPointer)) // WIC annotation is currently wrong on GetDataPointer wicPointer arg
							{
								CopyMemory(scanline, wicPointer, length);
							}
						}
					}

					if (SUCCEEDED(hr))
					{
						_frame++;
						sample->AddRef();
						*outSample = sample;
					}
				}
			}
		}
		lock.reset();
	}

	buffer2D->Unlock2D();
	return hr;
}

bool FrameGenerator::EnsureSharedFrame()
{
	if (_sharedView)
		return true;

	// sem memória ainda: tenta de novo no máximo uma vez por segundo
	auto now = GetTickCount64();
	if (now - _sharedLastTry < 1000)
		return false;
	_sharedLastTry = now;

	auto mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, KONECTA_FRAME_NAME);
	if (!mapping)
	{
		// Quem cria é esta DLL dentro do serviço de câmera (Frame Server), que
		// tem SeCreateGlobalPrivilege. O KONECTA é processo comum: medido, ele
		// recebe erro 5 ao criar em Global\ e só consegue abrir. Por isso o
		// DACL libera o usuário autenticado para escrever.
		PSECURITY_DESCRIPTOR sd = nullptr;
		if (ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;AU)", SDDL_REVISION_1, &sd, nullptr))
		{
			SECURITY_ATTRIBUTES sa{ sizeof(sa), sd, FALSE };
			mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, KONECTA_FRAME_SIZE, KONECTA_FRAME_NAME);
			LocalFree(sd);
		}
	}
	if (!mapping)
		return false; // carregada num processo sem o privilégio e memória ainda não criada: normal

	auto view = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, KONECTA_FRAME_SIZE);
	if (!view)
	{
		CloseHandle(mapping);
		return false;
	}
	_sharedMapping = mapping;
	_sharedView = (const BYTE*)view;
	WINTRACE(L"FrameGenerator: memoria do KONECTA mapeada");
	return true;
}

bool FrameGenerator::DrawSharedFrame()
{
	if (!EnsureSharedFrame())
		return false;

	auto header = (const KonectaFrameHeader*)_sharedView;
	if (header->magic != KONECTA_FRAME_MAGIC)
		return false; // o KONECTA ainda não escreveu nada

	UINT w = header->width, h = header->height, stride = header->stride;
	if (!w || !h || w > KONECTA_FRAME_MAX_W || h > KONECTA_FRAME_MAX_H || stride < w * 4 || stride * h > KONECTA_FRAME_MAX_W * KONECTA_FRAME_MAX_H * 4)
		return false;

	// KONECTA fechou ou travou: o aviso é melhor que uma imagem congelada
	if (GetTickCount64() - (ULONGLONG)header->timestampMs > KONECTA_FRAME_STALE_MS)
		return false;

	// par = quadro completo; ímpar = o KONECTA está escrevendo agora, então
	// reaproveita o último quadro em vez de pegar um pela metade
	auto sequence = ReadAcquire64(&header->sequence);
	if (!(sequence & 1))
	{
		auto size = _frameBitmap ? _frameBitmap->GetPixelSize() : D2D1::SizeU(0, 0);
		if (!_frameBitmap || size.width != w || size.height != h)
		{
			_frameBitmap.reset();
			auto props = D2D1::BitmapProperties(D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_IGNORE));
			if (FAILED(_renderTarget->CreateBitmap(D2D1::SizeU(w, h), props, &_frameBitmap)))
				return false;
		}
		if (FAILED(_frameBitmap->CopyFromMemory(nullptr, _sharedView + KONECTA_FRAME_HEADER, stride)))
			return false;
	}
	if (!_frameBitmap)
		return false;

	_renderTarget->DrawBitmap(_frameBitmap.get(), D2D1::RectF(0, 0, (FLOAT)_width, (FLOAT)_height), 1.0f, D2D1_BITMAP_INTERPOLATION_MODE_LINEAR);
	return true;
}
